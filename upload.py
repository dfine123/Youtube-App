#!/usr/bin/env python3
"""Main upload runner CLI for YouTube Shorts Uploader."""

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from src.database import Database, Creator
from src.oauth import OAuthManager
from src.sheets import SheetsManager, VideoEntry
from src.downloader import DriveDownloader, DownloadError
from src.uploader import YouTubeUploader, UploadResult, QuotaExceededError, estimate_quota_usage
from src.metadata import generate_shorts_metadata
from src.utils import (
    get_config,
    get_runway_emoji,
    format_duration,
    format_file_size,
    setup_logging
)

console = Console()
logger = setup_logging('upload')


class UploadRunner:
    """Orchestrates the upload process for a creator."""

    def __init__(self, creator: Creator, db: Database, dry_run: bool = False):
        """Initialize upload runner."""
        self.creator = creator
        self.db = db
        self.dry_run = dry_run
        self.config = get_config()

        # Initialize credentials
        self.oauth = OAuthManager()
        self.credentials = None
        self._youtube = None
        self._sheets = None
        self._downloader = None

    def _get_credentials(self):
        """Get valid credentials, refreshing if needed."""
        if self.credentials is None:
            self.credentials, was_refreshed = self.oauth.get_valid_credentials(
                self.creator.access_token,
                self.creator.refresh_token,
                self.creator.token_expiry
            )

            if was_refreshed:
                # Update tokens in database
                expiry = self.credentials.expiry.replace(tzinfo=None) if self.credentials.expiry else None
                self.db.update_tokens(
                    self.creator.name,
                    self.credentials.token,
                    token_expiry=expiry
                )

        return self.credentials

    @property
    def youtube(self) -> YouTubeUploader:
        """Get YouTube uploader instance."""
        if self._youtube is None:
            self._youtube = YouTubeUploader(self._get_credentials())
        return self._youtube

    @property
    def sheets(self) -> SheetsManager:
        """Get Sheets manager instance."""
        if self._sheets is None:
            self._sheets = SheetsManager(self._get_credentials(), self.creator.sheet_id)
        return self._sheets

    @property
    def downloader(self) -> DriveDownloader:
        """Get Drive downloader instance."""
        if self._downloader is None:
            self._downloader = DriveDownloader(self._get_credentials())
        return self._downloader

    def show_header(self):
        """Display header with creator info."""
        can_upload, reason = self.db.can_upload(self.creator)
        stats = self.db.get_content_stats(self.creator.id)

        shorts_ready = stats.shorts_pending if stats else 0
        days_runway = stats.days_runway if stats else 0
        emoji = get_runway_emoji(days_runway)

        next_slot = "Available now" if can_upload else reason

        header = f"""[bold]Creator:[/bold] {self.creator.name}
[bold]Channel:[/bold] {self.creator.channel_id}
[bold]Today:[/bold] {self.creator.uploads_today}/{self.creator.posts_per_day} uploaded | Next slot: {next_slot}
[bold]Shorts ready:[/bold] {shorts_ready} | [bold]Runway:[/bold] {emoji} {days_runway:.0f} days"""

        if self.dry_run:
            header = "[yellow][DRY RUN MODE][/yellow]\n" + header

        console.print(Panel(header, title="YouTube Shorts Uploader", border_style="cyan"))

    def run(self, limit: Optional[int] = None, force: bool = False) -> dict:
        """
        Run the upload process.

        Args:
            limit: Maximum number of videos to upload
            force: Ignore timing restrictions

        Returns:
            Dict with upload statistics
        """
        results = {
            'uploaded': 0,
            'skipped': 0,
            'failed': 0,
            'quota_used': 0,
            'errors': []
        }

        # Check if we can upload
        can_upload, reason = self.db.can_upload(self.creator, force)
        if not can_upload:
            console.print(f"[yellow]Cannot upload:[/yellow] {reason}")
            return results

        # Calculate how many we can upload
        uploads_remaining = self.db.get_uploads_remaining_today(self.creator)
        if limit:
            uploads_remaining = min(uploads_remaining, limit)

        if uploads_remaining == 0:
            console.print("[yellow]No uploads remaining for today.[/yellow]")
            return results

        # Get videos to upload
        shorts, too_long = self.sheets.get_videos_to_upload(uploads_remaining)

        if not shorts and not too_long:
            console.print("[yellow]No pending videos found.[/yellow]")
            return results

        # Log skipped videos
        for video in too_long:
            self._handle_skipped(video, results)

        if not shorts:
            console.print("[yellow]No valid Shorts found (all videos too long).[/yellow]")
            return results

        console.print()

        # Process each video
        for i, video in enumerate(shorts[:uploads_remaining], 1):
            console.print(f"[bold][{i}/{len(shorts[:uploads_remaining])}][/bold] Post ID: {video.post_id}")
            console.print(f"      Duration: {format_duration(video.duration)} [green]\u2713[/green] (valid Short)")

            if self.dry_run:
                console.print("      [yellow]\u2192 Would upload as Short[/yellow]")
                results['uploaded'] += 1
                console.print()
                continue

            try:
                result = self._upload_video(video)

                if result.success:
                    results['uploaded'] += 1
                    results['quota_used'] += result.quota_used

                    # Update database
                    self.db.record_upload(
                        self.creator.id,
                        video.post_id,
                        result.video_id,
                        'success',
                        video_duration=video.duration
                    )
                    self.db.update_upload_count(self.creator.id)

                    # Update sheet
                    self.sheets.mark_as_uploaded(video.row_number, result.video_id)

                    console.print(f"      [green]\u2713 Uploaded:[/green] {result.shorts_url}")
                    console.print(f"      [green]\u2713 Sheet updated[/green] (Posted to YT = TRUE)")
                else:
                    results['failed'] += 1
                    results['errors'].append(f"{video.post_id}: {result.error_message}")

                    # Record failure
                    self.db.record_upload(
                        self.creator.id,
                        video.post_id,
                        None,
                        'failed',
                        result.error_message,
                        video.duration
                    )

                    # Update sheet
                    self.sheets.mark_as_failed(video.row_number, result.error_message)

                    console.print(f"      [red]\u2717 Failed:[/red] {result.error_message}")

            except QuotaExceededError:
                console.print("[red]YouTube API quota exceeded. Stopping uploads.[/red]")
                results['errors'].append("Quota exceeded")
                break

            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"{video.post_id}: {e}")
                console.print(f"      [red]\u2717 Error:[/red] {e}")
                logger.exception(f"Upload error for {video.post_id}")

            console.print()

        return results

    def _upload_video(self, video: VideoEntry) -> UploadResult:
        """Download and upload a single video."""
        # Download from Drive
        console.print("      Downloading from Drive...")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=True
        ) as progress:
            download_task = progress.add_task("Downloading...", total=100)

            def download_progress(pct):
                progress.update(download_task, completed=int(pct * 100))

            video_path = self.downloader.download_from_url(
                video.video_link,
                progress_callback=download_progress
            )

        file_size = video_path.stat().st_size
        console.print(f"      [green]\u2713 Downloaded[/green] ({format_file_size(file_size)})")

        try:
            # Generate metadata
            metadata = generate_shorts_metadata(
                caption=video.caption,
                post_id=video.post_id,
                original_url=video.original_url,
                privacy_status=self.creator.default_privacy
            )

            # Upload to YouTube
            console.print("      Uploading to YouTube...")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
                transient=True
            ) as progress:
                upload_task = progress.add_task("Uploading...", total=100)

                def upload_progress(pct):
                    progress.update(upload_task, completed=int(pct * 100))

                result = self.youtube.upload_video(
                    video_path,
                    metadata,
                    progress_callback=upload_progress
                )

            return result

        finally:
            # Clean up temp file
            self.downloader.cleanup_temp_file(video_path)

    def _handle_skipped(self, video: VideoEntry, results: dict):
        """Handle a skipped video (too long)."""
        results['skipped'] += 1

        # Record in database
        self.db.record_upload(
            self.creator.id,
            video.post_id,
            None,
            'skipped_too_long',
            f"Duration {format_duration(video.duration)} exceeds 60s limit",
            video.duration
        )

        # Update sheet
        if not self.dry_run:
            self.sheets.mark_as_skipped(video.row_number, 'skipped_too_long')

        logger.info(f"Skipped {video.post_id}: duration {video.duration}s exceeds limit")

    def show_summary(self, results: dict):
        """Display upload summary."""
        # Refresh creator data
        creator = self.db.get_creator(self.creator.name)
        stats = self.db.get_content_stats(self.creator.id)

        uploads_remaining = self.db.get_uploads_remaining_today(creator)
        progress_str = f"{creator.uploads_today}/{creator.posts_per_day}"

        if uploads_remaining == 0:
            progress_str += " [green]\u2713 COMPLETE[/green]"
            next_upload = "Tomorrow"
        else:
            next_upload = "Available now"

        shorts_ready = stats.shorts_pending - results['uploaded'] if stats else 0
        days_runway = shorts_ready / creator.posts_per_day if creator.posts_per_day > 0 else 0
        emoji = get_runway_emoji(days_runway)

        # Estimate quota
        quota = estimate_quota_usage(results['uploaded'])

        summary = f"""[bold]Uploaded:[/bold] {results['uploaded']} Shorts
[bold]Skipped:[/bold] {results['skipped']}
[bold]Failed:[/bold] {results['failed']}

[bold]Today's Progress:[/bold] {progress_str}
[bold]Next upload:[/bold] {next_upload}

[bold]Updated Runway:[/bold] {emoji} {days_runway:.0f} days ({shorts_ready} Shorts remaining)
[bold]Quota used:[/bold] ~{quota['total']:,} units ({quota['percent_used']:.0f}% of daily)"""

        if results['errors']:
            summary += "\n\n[bold red]Errors:[/bold red]"
            for error in results['errors'][:5]:  # Show max 5 errors
                summary += f"\n  \u2022 {error}"

        console.print(Panel(summary, title="Summary", border_style="green"))


@click.command()
@click.option('--creator', '-c', help='Creator name to process')
@click.option('--all', '-a', 'all_creators', is_flag=True, help='Process all creators')
@click.option('--limit', '-l', type=int, help='Maximum number of videos to upload')
@click.option('--dry-run', '-n', is_flag=True, help='Show what would happen without uploading')
@click.option('--force', '-f', is_flag=True, help='Ignore timing restrictions')
@click.option('--status', '-s', is_flag=True, help='Show queue status without uploading')
def main(creator: str, all_creators: bool, limit: int, dry_run: bool, force: bool, status: bool):
    """Upload YouTube Shorts for creators."""
    db = Database()

    # Reset daily counts
    db.reset_daily_counts()

    if status:
        show_queue_status(db)
        return

    if not creator and not all_creators:
        console.print("[red]Error:[/red] Specify --creator NAME or --all")
        console.print("\nExamples:")
        console.print("  python upload.py --creator 'CreatorName'")
        console.print("  python upload.py --all")
        console.print("  python upload.py --creator 'CreatorName' --dry-run")
        sys.exit(1)

    if creator:
        # Process single creator
        creator_obj = db.get_creator(creator)
        if not creator_obj:
            console.print(f"[red]Error:[/red] Creator '{creator}' not found.")
            sys.exit(1)

        process_creator(creator_obj, db, limit, dry_run, force)

    elif all_creators:
        # Process all creators
        creators = db.get_all_creators()

        if not creators:
            console.print("[yellow]No active creators found.[/yellow]")
            return

        for i, creator_obj in enumerate(creators):
            if i > 0:
                console.print()
                console.print("-" * 60)
                console.print()

            process_creator(creator_obj, db, limit, dry_run, force)


def process_creator(creator: Creator, db: Database, limit: int, dry_run: bool, force: bool):
    """Process uploads for a single creator."""
    try:
        runner = UploadRunner(creator, db, dry_run)
        runner.show_header()
        console.print()

        results = runner.run(limit, force)

        console.print()
        runner.show_summary(results)

    except Exception as e:
        console.print(f"[red]Error processing {creator.name}:[/red] {e}")
        logger.exception(f"Error processing {creator.name}")


def show_queue_status(db: Database):
    """Show upload queue status for all creators."""
    creators = db.get_all_creators()

    if not creators:
        console.print("[yellow]No active creators found.[/yellow]")
        return

    console.print(Panel.fit(
        "[bold cyan]Upload Queue Status[/bold cyan]",
        border_style="cyan"
    ))
    console.print()

    for creator in creators:
        can_upload, reason = db.can_upload(creator)
        stats = db.get_content_stats(creator.id)

        shorts_ready = stats.shorts_pending if stats else 0
        days_runway = stats.days_runway if stats else 0
        emoji = get_runway_emoji(days_runway)

        status_icon = "[green]\u2713[/green]" if can_upload else "[yellow]\u23f8[/yellow]"
        status_text = "Ready" if can_upload else reason

        console.print(f"{status_icon} [bold]{creator.name}[/bold]")
        console.print(f"   Today: {creator.uploads_today}/{creator.posts_per_day} | {status_text}")
        console.print(f"   Queue: {shorts_ready} Shorts | Runway: {emoji} {days_runway:.0f} days")
        console.print()


if __name__ == '__main__':
    main()
