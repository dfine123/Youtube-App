"""Generate metadata (title, description, tags) for YouTube Shorts uploads."""

import re
from typing import List, Optional
from dataclasses import dataclass

from .utils import get_config, setup_logging

logger = setup_logging('metadata')


@dataclass
class VideoMetadata:
    """Metadata for a YouTube video upload."""
    title: str
    description: str
    tags: List[str]
    category_id: str
    privacy_status: str

    def to_youtube_body(self) -> dict:
        """Convert to YouTube API request body format."""
        return {
            'snippet': {
                'title': self.title,
                'description': self.description,
                'tags': self.tags,
                'categoryId': self.category_id,
            },
            'status': {
                'privacyStatus': self.privacy_status,
                'selfDeclaredMadeForKids': False,
            }
        }


class MetadataGenerator:
    """Generates optimized metadata for YouTube Shorts."""

    def __init__(self, privacy_status: Optional[str] = None):
        """Initialize generator with optional privacy override."""
        self.config = get_config()
        self.default_privacy = privacy_status or self.config['defaults']['privacy_status']
        self.default_category = self.config['defaults']['category_id']

    def generate(
        self,
        caption: str,
        post_id: str,
        original_url: Optional[str] = None,
        privacy_status: Optional[str] = None
    ) -> VideoMetadata:
        """
        Generate metadata for a YouTube Short.

        Args:
            caption: Original caption from the video
            post_id: Unique post identifier
            original_url: URL to the original post (e.g., Instagram)
            privacy_status: Override privacy setting

        Returns:
            VideoMetadata object ready for upload
        """
        title = self._generate_title(caption, post_id)
        description = self._generate_description(caption, original_url)
        tags = self._extract_tags(caption)

        return VideoMetadata(
            title=title,
            description=description,
            tags=tags,
            category_id=self.default_category,
            privacy_status=privacy_status or self.default_privacy
        )

    def _generate_title(self, caption: str, post_id: str) -> str:
        """
        Generate a YouTube-friendly title.

        Rules:
        - Prepend "#Shorts " for algorithm discovery
        - Use caption if available, otherwise Post ID
        - Limit to 90 characters total
        - Clean up special characters
        """
        prefix = "#Shorts "
        max_length = 100 - len(prefix)  # YouTube limit is 100 chars

        if caption and caption.strip():
            # Clean up the caption for title use
            title_text = self._clean_for_title(caption)

            # Truncate if needed
            if len(title_text) > max_length:
                title_text = title_text[:max_length - 3] + "..."
        else:
            # Fall back to Post ID
            title_text = f"Video {post_id}"

        return f"{prefix}{title_text}"

    def _clean_for_title(self, text: str) -> str:
        """Clean text for use in a YouTube title."""
        # Remove URLs
        text = re.sub(r'https?://\S+', '', text)

        # Remove hashtags (they'll be in description/tags)
        text = re.sub(r'#\w+', '', text)

        # Remove multiple spaces
        text = re.sub(r'\s+', ' ', text)

        # Remove special characters that YouTube doesn't like
        text = re.sub(r'[<>]', '', text)

        # Remove leading/trailing whitespace and newlines
        text = text.strip()

        # Take first sentence or line if it's long
        if len(text) > 80:
            # Try to cut at sentence end
            match = re.match(r'^([^.!?]+[.!?])', text)
            if match and len(match.group(1)) <= 80:
                text = match.group(1)
            else:
                # Cut at first newline
                text = text.split('\n')[0].strip()

        return text

    def _generate_description(
        self,
        caption: str,
        original_url: Optional[str]
    ) -> str:
        """
        Generate YouTube description.

        Includes:
        - Full original caption
        - Link to original post
        - Hashtags for discoverability
        """
        parts = []

        # Add caption if available
        if caption and caption.strip():
            parts.append(caption.strip())

        # Add original source link
        if original_url:
            parts.append(f"\nOriginal: {original_url}")

        # Add default hashtags for Shorts discoverability
        shorts_tags = "\n\n#Shorts #viral #trending"
        parts.append(shorts_tags)

        description = '\n'.join(parts)

        # YouTube description limit is 5000 characters
        if len(description) > 5000:
            description = description[:4997] + "..."

        return description

    def _extract_tags(self, caption: str) -> List[str]:
        """
        Extract hashtags from caption and convert to tags.

        Always includes 'shorts' tag for algorithm.
        """
        tags = ['shorts', 'short', 'viral']

        if caption:
            # Find all hashtags
            hashtags = re.findall(r'#(\w+)', caption)

            for tag in hashtags:
                # Normalize tag
                tag_lower = tag.lower()

                # Skip if already included or too short
                if tag_lower not in [t.lower() for t in tags] and len(tag) >= 2:
                    tags.append(tag)

        # YouTube allows up to 500 chars total for tags
        # Keep truncating until we're under limit
        while sum(len(t) for t in tags) + len(tags) - 1 > 500 and len(tags) > 3:
            tags.pop()

        return tags


def generate_shorts_metadata(
    caption: str,
    post_id: str,
    original_url: Optional[str] = None,
    privacy_status: Optional[str] = None
) -> VideoMetadata:
    """
    Convenience function to generate Shorts metadata.

    Args:
        caption: Original video caption
        post_id: Unique identifier for the post
        original_url: Link to original source
        privacy_status: Override default privacy setting

    Returns:
        VideoMetadata ready for YouTube upload
    """
    generator = MetadataGenerator(privacy_status)
    return generator.generate(caption, post_id, original_url, privacy_status)
