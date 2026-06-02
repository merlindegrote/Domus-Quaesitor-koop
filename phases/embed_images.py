"""Embed images as base64 data URIs for email delivery (bypass CDN hotlink blocking)"""
import base64, io, logging, os, time
import urllib.request

logger = logging.getLogger(__name__)

def _fetch_with_retry(url: str, max_retries: int = 2, timeout: int = 10) -> bytes | None:
    """Download image with proper headers to bypass basic CDN blocking"""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    ]
    
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": user_agents[attempt % len(user_agents)],
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
                    "Referer": "https://www.google.com/",
                    "Sec-Fetch-Dest": "image",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": "cross-site",
                }
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if len(data) > 500:  # minimum viable image
                    return data
                logger.debug(f"Image too small ({len(data)} bytes): {url[:60]}")
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1)
                continue
            logger.debug(f"Failed to fetch image after {max_retries+1} attempts: {e}")
            return None
    return None

def embed_images(listings: list[dict]) -> list[dict]:
    """Download first image per listing and embed as base64 data URI.
    Returns listings with embedded image_urls. Falls back to original URL if download fails."""
    
    total = len(listings)
    embedded = 0
    failed = 0
    
    for i, listing in enumerate(listings):
        urls = listing.get("image_urls", [])
        if not urls:
            continue
        
        original_url = urls[0]
        if original_url.startswith("data:"):
            embedded += 1
            continue  # already embedded
        
        data = _fetch_with_retry(original_url)
        if data:
            # Detect MIME type from content or fallback
            mime = _guess_mime(data, original_url)
            b64 = base64.b64encode(data).decode("ascii")
            listing["image_urls"] = [f"data:{mime};base64,{b64}"]
            embedded += 1
        else:
            failed += 1
        
        if (i + 1) % 10 == 0:
            logger.info(f"  Embed: {i+1}/{total} done ({embedded} ok, {failed} failed)")
    
    logger.info(f"✅ Image embed: {embedded} embedded, {failed} failed (out of {total})")
    return listings

def _guess_mime(data: bytes, url: str) -> str:
    """Guess MIME type from image headers or URL extension"""
    # Check magic bytes
    if data[:4] == b'\x89PNG':
        return "image/png"
    if data[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    if data[:4] == b'RIFF' and data[8:12] == b'AVIF':
        return "image/avif"
    
    # Fallback to URL extension
    ext = url.lower().rsplit('.', 1)[-1] if '.' in url else ''
    return {
        'jpg': "image/jpeg",
        'jpeg': "image/jpeg",
        'png': "image/png",
        'gif': "image/gif",
        'webp': "image/webp"
    }.get(ext, "image/jpeg")
