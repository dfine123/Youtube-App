#!/usr/bin/env python3
"""Creator management CLI for YouTube Shorts Uploader."""

import sys
from datetime import datetime

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt, Confirm

from src.database import Database, Creator
from src.oauth import OAuthManager, YouTubeClient, SheetsClient, verify_all_connections
from src.sheets import SheetsManager
from src.utils import (
    get_config,
    get_runway_emoji,
    time_since,
    time_until,
    setup_logging
)

console = Console()
logger = setup_logging('manage')


@click.group()
def cli():
    """YouTube Shorts Uploader - Creator Management"""
    pass


@cli.command('add-creator')
def add_creator():
    """Add a new creator with OAuth authorization."""
    console.print(Panel.fit(
        "[bold cyan]Add New Creator[/bold cyan]",
        border_style="cyan"
    ))

    db = Database()

    # Get creator details
    console.print()
    name = Prompt.ask("[bold]Creator Name[/bold]")

    # Check if creator already exists
    if db.get_creator(name):
        console.print(f"[red]Error:[/red] Creator '{name}' already exists.")
        console.print("Use [cyan]python manage.py reauth[/cyan] to re-authenticate.")
        return

    sheet_id = Prompt.ask("[bold]Google Sheet ID[/bold]")
    posts_per_day = IntPrompt.ask(
        "[bold]Posts per day[/bold]",
        default=3,
        show_default=True
    )

    # Validate posts per day
    if posts_per_day < 1 or posts_per_day > 10:
        console.print("[red]Error:[/red] Posts per day must be between 1 and 10.")
        return

    console.print()
    console.print("[yellow]Opening browser for YouTube authorization...[/yellow]")
    console.print("[dim]Please authorize access to your YouTube channel.[/dim]")
    console.print()

    try:
        # Run OAuth flow
        oauth = OAuthManager()
        access_token, refresh_token, token_expiry = oauth.run_oauth_flow()

        console.print("[green]\u2713 Authorization successful[/green]")

        # Get credentials and verify access
        credentials, _ = oauth.get_valid_credentials(
            access_token, refresh_token, token_expiry
        )

        # Get channel info
        youtube = YouTubeClient(credentials)
        channel_info = youtube.get_channel_info()

        if not channel_info:
            console.print("[red]Error:[/red] No YouTube channel found for this account.")
            return

        console.print(
            f"[green]\u2713 Channel found:[/green] {channel_info['title']} ({channel_info['id']})"
        )

        # Verify sheet access
        sheets_client = SheetsClient(credentials)
        if not sheets_client.verify_sheet_access(sheet_id):
            console.print(f"[red]Error:[/red] Cannot access sheet {sheet_id}")
            console.print("Make sure the sheet is shared with your Google account.")
            return

        console.print("[green]\u2713 Sheet access verified[/green]")

        # Get content stats from sheet
        sheets = SheetsManager(credentials, sheet_id)
        stats = sheets.get_content_stats()

        console.print(f"  [dim]\u2192 {stats['total_pending']} total pending videos[/dim]")
        console.print(f"  [dim]\u2192 {stats['shorts_pending']} valid Shorts (<60s)[/dim]")
        console.print(f"  [dim]\u2192 {stats['skipped_too_long']} skipped (too long)[/dim]")

        # Save creator to database
        config = get_config()
        creator_id = db.add_creator(
            name=name,
            channel_id=channel_info['id'],
            sheet_id=sheet_id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=token_expiry,
            posts_per_day=posts_per_day,
            default_privacy=config['defaults']['privacy_status']
        )

        # Update content stats
        db.update_content_stats(
            creator_id,
            stats['total_pending'],
            stats['shorts_pending'],
            stats['skipped_too_long']
        )

        console.print("[green]\u2713 Creator profile saved[/green]")

        # Show runway
        days_runway = stats['shorts_pending'] / posts_per_day if posts_per_day > 0 else 0
        emoji = get_runway_emoji(days_runway)

        console.print()
        console.print(Panel.fit(
            f"[bold]\U0001f4ca Content Runway:[/bold] {emoji} {days_runway:.0f} days "
            f"({stats['shorts_pending']} Shorts \u00f7 {posts_per_day}/day)",
            border_style="green"
        ))

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        logger.exception("Failed to add creator")
        return


@cli.command('list')
def list_creators():
    """List all creators with content runway."""
    db = Database()
    creators = db.get_all_creators(active_only=False)

    if not creators:
        console.print("[yellow]No creators found.[/yellow]")
        console.print("Add one with: [cyan]python manage.py add-creator[/cyan]")
        return

    table = Table(title="Creator Dashboard", border_style="cyan")
    table.add_column("Creator", style="bold")
    table.add_column("Posts/Day", justify="center")
    table.add_column("Shorts Ready", justify="center")
    table.add_column("Runway", justify="center")
    table.add_column("Status", justify="center")

    for creator in creators:
        stats = db.get_content_stats(creator.id)

        shorts_ready = stats.shorts_pending if stats else 0
        days_runway = stats.days_runway if stats else 0

        emoji = get_runway_emoji(days_runway)
        runway_str = f"{emoji} {days_runway:.0f} days"

        status = "[green]Active[/green]" if creator.active else "[red]Inactive[/red]"

        # Check if needs re-auth
        if not creator.refresh_token:
            status = "[yellow]Needs Auth[/yellow]"

        table.add_row(
            creator.name,
            f"{creator.posts_per_day}/day",
            str(shorts_ready),
            runway_str,
            status
        )

    console.print()
    console.print(table)
    console.print()
    console.print(
        "[dim]Legend: \U0001f7e2 14+ days | \U0001f7e1 7-13 days | "
        "\U0001f534 <7 days | \u26ab Empty[/dim]"
    )


@cli.command('status')
def status():
    """Show detailed status for all creators."""
    db = Database()
    creators = db.get_all_creators()

    if not creators:
        console.print("[yellow]No active creators found.[/yellow]")
        return

    for creator in creators:
        _show_creator_status(db, creator)
        console.print()


@cli.command('show')
@click.argument('name')
def show_creator(name: str):
    """Show details for a specific creator."""
    db = Database()
    creator = db.get_creator(name)

    if not creator:
        console.print(f"[red]Error:[/red] Creator '{name}' not found.")
        return

    _show_creator_status(db, creator)


def _show_creator_status(db: Database, creator: Creator):
    """Display detailed status for a creator."""
    stats = db.get_content_stats(creator.id)

    # Calculate next upload time
    can_upload, reason = db.can_upload(creator)

    if can_upload:
        next_upload = "Available now"
    elif "quota" in reason.lower():
        next_upload = "Tomorrow"
    else:
        next_upload = reason

    # Format today's progress
    uploads_remaining = db.get_uploads_remaining_today(creator)
    progress_str = f"{creator.uploads_today}/{creator.posts_per_day} uploaded"
    if uploads_remaining == 0:
        progress_str += " [green]\u2713 COMPLETE[/green]"

    # Format last upload time
    last_upload_str = time_since(creator.last_upload_at) if creator.last_upload_at else "Never"

    # Get runway info
    shorts_ready = stats.shorts_pending if stats else 0
    days_runway = stats.days_runway if stats else 0
    skipped = stats.skipped_too_long if stats else 0
    emoji = get_runway_emoji(days_runway)

    content = f"""[bold]Channel:[/bold] {creator.channel_id}
[bold]Posts/Day:[/bold] {creator.posts_per_day} (spaced 2 hours apart)

[bold]Today's Progress:[/bold] {progress_str}
[bold]Last Upload:[/bold] {last_upload_str}
[bold]Next Upload:[/bold] {next_upload}

[bold]Content Runway:[/bold]
  \u2192 Shorts ready: {shorts_ready}
  \u2192 At {creator.posts_per_day}/day: {emoji} {days_runway:.0f} days remaining
  \u2192 Videos skipped (>60s): {skipped}"""

    console.print(Panel(
        content,
        title=f"[bold cyan]{creator.name}[/bold cyan]",
        border_style="cyan"
    ))


@cli.command('update')
@click.argument('name')
@click.option('--posts-per-day', '-p', type=int, help='Set posts per day (1-10)')
@click.option('--privacy', type=click.Choice(['public', 'private', 'unlisted']),
              help='Set default privacy status')
@click.option('--active/--inactive', default=None, help='Enable/disable creator')
def update_creator(name: str, posts_per_day: int, privacy: str, active: bool):
    """Update creator settings."""
    db = Database()
    creator = db.get_creator(name)

    if not creator:
        console.print(f"[red]Error:[/red] Creator '{name}' not found.")
        return

    updates = {}

    if posts_per_day is not None:
        if posts_per_day < 1 or posts_per_day > 10:
            console.print("[red]Error:[/red] Posts per day must be between 1 and 10.")
            return
        updates['posts_per_day'] = posts_per_day

    if privacy is not None:
        updates['default_privacy'] = privacy

    if active is not None:
        updates['active'] = active

    if not updates:
        console.print("[yellow]No updates specified.[/yellow]")
        console.print("Options: --posts-per-day, --privacy, --active/--inactive")
        return

    if db.update_creator(name, **updates):
        console.print(f"[green]\u2713 Updated {name}:[/green]")
        for key, value in updates.items():
            console.print(f"  {key}: {value}")
    else:
        console.print("[red]Error:[/red] Failed to update creator.")


@cli.command('remove')
@click.argument('name')
def remove_creator(name: str):
    """Remove a creator from the database."""
    db = Database()
    creator = db.get_creator(name)

    if not creator:
        console.print(f"[red]Error:[/red] Creator '{name}' not found.")
        return

    if not Confirm.ask(f"Are you sure you want to remove [bold]{name}[/bold]?"):
        console.print("Cancelled.")
        return

    if db.remove_creator(name):
        console.print(f"[green]\u2713 Removed creator:[/green] {name}")
    else:
        console.print("[red]Error:[/red] Failed to remove creator.")


@cli.command('reauth')
@click.argument('name')
def reauth_creator(name: str):
    """Re-authenticate a creator (refresh OAuth tokens)."""
    db = Database()
    creator = db.get_creator(name)

    if not creator:
        console.print(f"[red]Error:[/red] Creator '{name}' not found.")
        return

    console.print(f"[yellow]Re-authenticating {name}...[/yellow]")
    console.print("[dim]Please authorize access in your browser.[/dim]")
    console.print()

    try:
        oauth = OAuthManager()
        access_token, refresh_token, token_expiry = oauth.run_oauth_flow()

        # Update tokens in database
        db.update_tokens(name, access_token, refresh_token, token_expiry)

        console.print(f"[green]\u2713 Re-authenticated {name}[/green]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        logger.exception("Failed to re-authenticate")


@cli.command('test')
@click.argument('name')
def test_connection(name: str):
    """Test all connections for a creator."""
    db = Database()
    creator = db.get_creator(name)

    if not creator:
        console.print(f"[red]Error:[/red] Creator '{name}' not found.")
        return

    console.print(f"[cyan]Testing connections for {name}...[/cyan]")
    console.print()

    results = verify_all_connections(
        creator.access_token,
        creator.refresh_token,
        creator.token_expiry,
        creator.sheet_id
    )

    # Display results
    if results['credentials_valid']:
        console.print("[green]\u2713 OAuth credentials valid[/green]")
    else:
        console.print("[red]\u2717 OAuth credentials invalid[/red]")

    if results['youtube_access']:
        channel = results['channel_info']
        console.print(f"[green]\u2713 YouTube access OK[/green] - {channel['title']}")
    else:
        console.print("[red]\u2717 YouTube access failed[/red]")

    if results['sheets_access']:
        console.print(f"[green]\u2713 Sheets access OK[/green] - {results['sheet_title']}")
    else:
        console.print("[red]\u2717 Sheets access failed[/red]")

    if results['errors']:
        console.print()
        console.print("[red]Errors:[/red]")
        for error in results['errors']:
            console.print(f"  - {error}")


@cli.command('refresh-stats')
def refresh_stats():
    """Refresh content statistics for all creators."""
    db = Database()
    creators = db.get_all_creators()

    if not creators:
        console.print("[yellow]No active creators found.[/yellow]")
        return

    console.print("[cyan]Refreshing content stats...[/cyan]")
    console.print()

    for creator in creators:
        try:
            # Get fresh credentials
            oauth = OAuthManager()
            credentials, was_refreshed = oauth.get_valid_credentials(
                creator.access_token,
                creator.refresh_token,
                creator.token_expiry
            )

            # Update tokens if refreshed
            if was_refreshed:
                db.update_tokens(
                    creator.name,
                    credentials.token,
                    token_expiry=credentials.expiry.replace(tzinfo=None) if credentials.expiry else None
                )

            # Get stats from sheet
            sheets = SheetsManager(credentials, creator.sheet_id)
            stats = sheets.get_content_stats()

            # Update database
            db.update_content_stats(
                creator.id,
                stats['total_pending'],
                stats['shorts_pending'],
                stats['skipped_too_long']
            )

            days_runway = stats['shorts_pending'] / creator.posts_per_day
            emoji = get_runway_emoji(days_runway)

            console.print(
                f"[green]\u2713[/green] {creator.name}: {stats['shorts_pending']} Shorts "
                f"({emoji} {days_runway:.0f} days)"
            )

        except Exception as e:
            console.print(f"[red]\u2717[/red] {creator.name}: {e}")
            logger.exception(f"Failed to refresh stats for {creator.name}")

    console.print()
    console.print("[green]Stats refresh complete.[/green]")


if __name__ == '__main__':
    cli()
