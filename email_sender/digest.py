"""Email digest builder and sender for apartment listings."""

from __future__ import annotations

import html
import logging
import os
import smtplib
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTPUTF8

from scrapers.base import Listing
from config import TARGET_POSTAL_CODE, TARGET_CITY, MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)
MAILER_VERSION = "2026-04-04-2"


def _clean_text(value: str) -> str:
    """Normalize scraped text for email transport."""
    return (
        value.replace("\xa0", " ")
        .replace("\u202f", " ")
        .replace("\r\n", "\n")
    )


def _clean_email_field(value: str) -> str:
    """Normalize email header fields loaded from environment variables."""
    return _clean_text(value).strip()


def _ascii_safe(value: str) -> str:
    """Convert content to ASCII-safe text for older SMTP stacks."""
    normalized = unicodedata.normalize("NFKD", _clean_text(value))
    normalized = normalized.replace("EUR", "EUR")
    return normalized.encode("ascii", "ignore").decode("ascii")


def _score_color(score: float | None) -> str:
    if score is None:
        return "#9CA3AF"
    if score >= 8:
        return "#10B981"
    if score >= 6:
        return "#F59E0B"
    if score >= 4:
        return "#F97316"
    return "#EF4444"


def _score_emoji(score: float | None) -> str:
    if score is None:
        return "❔"
    if score >= 8:
        return "🔥"
    if score >= 6:
        return "✨"
    if score >= 4:
        return "🏠"
    return "⚠️"


def _platform_badge(platform: str) -> str:
    platform = _clean_text(platform)
    colors = {
        "immoweb": "#FF6B00",
        "zimmo": "#2563EB",
        "immoscoop": "#7C3AED",
    }
    color = colors.get(platform, "#6B7280")
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:4px;font-size:11px;font-weight:600;'
        f'text-transform:uppercase;letter-spacing:0.5px">{html.escape(platform)}</span>'
    )


def _build_listing_cards(listings: list[Listing]) -> str:
    cards_html = ""

    for index, listing in enumerate(listings, start=1):
        title = _clean_text(listing.title)
        address = _clean_text(listing.address)
        reasoning = _clean_text(listing.score_reasoning or "")
        score = listing.final_score
        score_display = f"{score:.1f}" if score is not None else "-"
        score_color = _score_color(score)
        score_emoji = _score_emoji(score)

        thumbnail_html = ""
        if listing.image_urls:
            image_url = listing.image_urls[0]
            if image_url and image_url.startswith("http"):
                thumbnail_html = (
                    f'<img src="{html.escape(image_url)}" alt="Listing photo" '
                    f'style="width:100%;max-width:280px;height:180px;'
                    f'object-fit:cover;border-radius:8px;margin-bottom:12px" />'
                )

        details: list[str] = []
        if listing.bedrooms:
            room_label = "bedroom" if listing.bedrooms == 1 else "bedrooms"
            details.append(f"🛏️ {listing.bedrooms} {room_label}")
        if listing.surface_m2:
            details.append(f"📐 {listing.surface_m2}m²")
        if listing.epc_label:
            details.append(f"⚡ EPC {listing.epc_label}")
        details_html = " &middot; ".join(html.escape(_ascii_safe(part)) for part in details) if details else ""

        score_parts: list[str] = []
        if listing.text_score is not None:
            score_parts.append(f"Text: {listing.text_score:.1f}")
        if listing.photo_score is not None:
            score_parts.append(f"Photo: {listing.photo_score:.1f}")
        score_breakdown = html.escape(_ascii_safe(" | ".join(score_parts))) if score_parts else ""

        reasoning_html = ""
        if reasoning and reasoning != "AI scoring unavailable — unranked":
            reasoning_html = (
                '<p style="margin:8px 0;font-size:13px;color:#6B7280;'
                'font-style:italic;line-height:1.4">'
                f'{html.escape(_ascii_safe(reasoning))}</p>'
            )

        cards_html += f"""
        <div style="background:#fff;border-radius:12px;padding:20px;
                    margin-bottom:16px;border:1px solid #E5E7EB;
                    box-shadow:0 1px 3px rgba(0,0,0,0.06)">
            <div style="display:flex;gap:16px;flex-wrap:wrap">
                <div style="flex-shrink:0">
                    {thumbnail_html}
                </div>
                <div style="flex:1;min-width:200px">
                    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
                        <span style="background:#F3F4F6;color:#374151;padding:2px 10px;
                                    border-radius:20px;font-size:13px;font-weight:700">
                            #{index}
                        </span>
                        <span style="background:{score_color};color:#fff;padding:4px 12px;
                                    border-radius:20px;font-size:14px;font-weight:700">
                            {score_emoji} {score_display}/10
                        </span>
                        {_platform_badge(listing.platform)}
                    </div>

                    <h3 style="margin:0 0 4px;font-size:16px;color:#111827;
                              font-weight:600;line-height:1.3">
                        {html.escape(_ascii_safe(title))}
                    </h3>

                    <p style="margin:0 0 6px;font-size:18px;font-weight:700;color:#059669">
                        EUR {listing.price}/mo
                    </p>
                    <p style="margin:0 0 6px;font-size:13px;color:#6B7280">
                        {html.escape(_ascii_safe(address))}
                    </p>

                    <p style="margin:0 0 4px;font-size:13px;color:#374151">
                        {details_html}
                    </p>

                    <p style="margin:4px 0;font-size:11px;color:#9CA3AF">
                        {score_breakdown}
                    </p>

                    {reasoning_html}

                    <a href="{html.escape(listing.url)}" target="_blank" rel="noopener"
                       style="display:inline-block;margin-top:8px;padding:8px 20px;
                              background:#2563EB;color:#fff;text-decoration:none;
                              border-radius:6px;font-size:13px;font-weight:600">
                        View Listing
                    </a>
                </div>
            </div>
        </div>
        """

    return cards_html


def _build_digest_shell(
    *,
    heading: str,
    subtitle: str,
    body_html: str,
    footer_text: str,
) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
    </head>
    <body style="margin:0;padding:0;background:#F9FAFB;font-family:-apple-system,
                BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif">
        <div style="max-width:640px;margin:0 auto;padding:24px 16px">
            <div style="background:linear-gradient(135deg,#1E40AF 0%,#7C3AED 100%);
                        border-radius:16px;padding:28px 24px;margin-bottom:24px;
                        text-align:center">
                <h1 style="margin:0;font-size:24px;color:#fff;font-weight:700">
                    {html.escape(heading)}
                </h1>
                <p style="margin:8px 0 0;color:rgba(255,255,255,0.85);font-size:14px">
                    {html.escape(subtitle)}
                </p>
            </div>

            {body_html}

            <div style="text-align:center;padding:20px;color:#9CA3AF;font-size:12px">
                <p>{html.escape(footer_text)}</p>
            </div>
        </div>
    </body>
    </html>
    """


def build_html_digest(listings: list[Listing], date_str: str) -> str:
    count = len(listings)
    city_formatted = TARGET_CITY.capitalize()
    footer = f"{city_formatted} ({TARGET_POSTAL_CODE}) | EUR {MIN_PRICE:,}-{MAX_PRICE:,} | {MIN_BEDROOMS}+ bedroom{'s' if MIN_BEDROOMS != 1 else ''}"

    if count == 0:
        body_html = """
        <div style="background:#fff;border-radius:12px;padding:32px;
                    border:1px solid #E5E7EB;text-align:center">
            <p style="font-size:40px;margin:0">😴</p>
            <h2 style="margin:12px 0 8px;color:#374151;font-size:18px">
                Quiet day in the market
            </h2>
            <p style="color:#6B7280;font-size:14px;margin:0">
                Nothing fresh hit your criteria today. We stay patient and strike when the right one shows up.
            </p>
        </div>
        """
        return _build_digest_shell(
            heading="Apartment Hunter",
            subtitle=f"{date_str} - no new opportunities today",
            body_html=body_html,
            footer_text=footer,
        )

    price_min = min(listing.price for listing in listings)
    price_max = max(listing.price for listing in listings)
    top_score = f"{listings[0].final_score:.1f}" if listings[0].final_score is not None else "-"

    body_html = f"""
    <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap">
        <div style="flex:1;min-width:120px;background:#fff;border-radius:10px;
                    padding:16px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:28px;font-weight:700;color:#2563EB">{count}</div>
            <div style="font-size:12px;color:#6B7280;margin-top:4px">New Listings</div>
        </div>
        <div style="flex:1;min-width:120px;background:#fff;border-radius:10px;
                    padding:16px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:28px;font-weight:700;color:#059669">
                EUR {price_min}-{price_max}
            </div>
            <div style="font-size:12px;color:#6B7280;margin-top:4px">Price Range</div>
        </div>
        <div style="flex:1;min-width:120px;background:#fff;border-radius:10px;
                    padding:16px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:28px;font-weight:700;color:#7C3AED">{top_score}</div>
            <div style="font-size:12px;color:#6B7280;margin-top:4px">Top Score</div>
        </div>
    </div>
    {_build_listing_cards(listings)}
    """

    return _build_digest_shell(
        heading="Apartment Hunter",
        subtitle=f"{date_str} - {count} fresh listing{'s' if count != 1 else ''} worth a look in {city_formatted}",
        body_html=body_html,
        footer_text=f"Daily digest for {footer}",
    )


def build_weekly_html_digest(listings: list[Listing], week_label: str) -> str:
    city_formatted = TARGET_CITY.capitalize()
    footer = f"{city_formatted} ({TARGET_POSTAL_CODE}) | EUR {MIN_PRICE:,}-{MAX_PRICE:,} | {MIN_BEDROOMS}+ bedroom{'s' if MIN_BEDROOMS != 1 else ''}"

    if not listings:
        body_html = """
        <div style="background:#fff;border-radius:12px;padding:32px;
                    border:1px solid #E5E7EB;text-align:center">
            <p style="font-size:40px;margin:0">📭</p>
            <h2 style="margin:12px 0 8px;color:#374151;font-size:18px">
                No breakout listings this week
            </h2>
            <p style="color:#6B7280;font-size:14px;margin:0">
                The market stayed flat this week. Better to wait than chase the wrong deal.
            </p>
        </div>
        """
        return _build_digest_shell(
            heading="Apartment Hunter Weekly",
            subtitle=f"{week_label} - no standout listings this week",
            body_html=body_html,
            footer_text=f"Weekly top 10 for {footer}",
        )

    price_min = min(listing.price for listing in listings)
    price_max = max(listing.price for listing in listings)
    top_score = f"{listings[0].final_score:.1f}" if listings[0].final_score is not None else "-"

    body_html = f"""
    <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap">
        <div style="flex:1;min-width:120px;background:#fff;border-radius:10px;
                    padding:16px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:28px;font-weight:700;color:#2563EB">{len(listings)}</div>
            <div style="font-size:12px;color:#6B7280;margin-top:4px">Top Picks</div>
        </div>
        <div style="flex:1;min-width:120px;background:#fff;border-radius:10px;
                    padding:16px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:28px;font-weight:700;color:#059669">
                EUR {price_min}-{price_max}
            </div>
            <div style="font-size:12px;color:#6B7280;margin-top:4px">Price Range</div>
        </div>
        <div style="flex:1;min-width:120px;background:#fff;border-radius:10px;
                    padding:16px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:28px;font-weight:700;color:#7C3AED">{top_score}</div>
            <div style="font-size:12px;color:#6B7280;margin-top:4px">Best Score</div>
        </div>
    </div>
    {_build_listing_cards(listings)}
    """

    return _build_digest_shell(
        heading="Apartment Hunter Weekly",
        subtitle=f"{week_label} - the {len(listings)} strongest opportunities of the week",
        body_html=body_html,
        footer_text=f"Weekly top 10 for {footer}",
    )


def _build_daily_plain_text(listings: list[Listing], date_str: str) -> str:
    lines = [f"Apartment Hunter - {date_str}", ""]
    if listings:
        lines.append(f"{len(listings)} fresh listing(s) just hit the board:")
        lines.append("")
        for index, listing in enumerate(listings, start=1):
            score = f"{listing.final_score:.1f}" if listing.final_score is not None else "-"
            lines.append(f"#{index} [{score}/10] {_ascii_safe(listing.title)}")
            lines.append(f"    EUR {listing.price}/mo - {_ascii_safe(listing.address)}")
            lines.append(f"    {listing.url}")
            lines.append("")
    else:
        lines.append("Quiet day. Nothing new matched your criteria.")
    return "\n".join(lines)


def _build_weekly_plain_text(listings: list[Listing], week_label: str) -> str:
    lines = [f"Apartment Hunter Weekly - {week_label}", ""]
    if listings:
        lines.append("These are the strongest opportunities from this week:")
        lines.append("")
        for index, listing in enumerate(listings, start=1):
            score = f"{listing.final_score:.1f}" if listing.final_score is not None else "-"
            lines.append(f"#{index} [{score}/10] {_ascii_safe(listing.title)}")
            lines.append(f"    EUR {listing.price}/mo - {_ascii_safe(listing.address)}")
            lines.append(f"    {listing.url}")
            lines.append("")
    else:
        lines.append("No standout listings this week.")
    return "\n".join(lines)


def _smtp_config() -> tuple[str, str, str, str]:
    return (
        _clean_email_field(os.environ.get("GMAIL_FROM", "")),
        os.environ.get("GMAIL_APP_PASSWORD", ""),
        _clean_email_field(os.environ.get("EMAIL_TO", "")),
        _clean_email_field(os.environ.get("EMAIL_CC", "")),
    )


def _send_email(subject_text: str, plain_text: str, html_body: str) -> bool:
    gmail_from, gmail_password, email_to, email_cc = _smtp_config()

    if not all([gmail_from, gmail_password, email_to]):
        logger.error(
            "Email configuration incomplete. Required: "
            "GMAIL_FROM, GMAIL_APP_PASSWORD, EMAIL_TO"
        )
        return False

    msg = EmailMessage(policy=SMTPUTF8)
    msg["Subject"] = subject_text
    msg["From"] = gmail_from
    msg["To"] = email_to
    if email_cc:
        msg["Cc"] = email_cc

    subject_text = _ascii_safe(subject_text)
    plain_text = _ascii_safe(plain_text)
    html_body = _ascii_safe(html_body)

    msg.set_content(plain_text, charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    recipients = [email_to]
    if email_cc:
        recipients.extend([addr.strip() for addr in email_cc.split(",") if addr.strip()])

    try:
        logger.info("Sending digest to %s (CC: %s) with mailer %s", email_to, email_cc or "none", MAILER_VERSION)
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(gmail_from, gmail_password)
            mail_options = ["SMTPUTF8"] if server.has_extn("smtputf8") else []
            server.sendmail(
                gmail_from,
                recipients,
                msg.as_bytes(policy=SMTPUTF8),
                mail_options=mail_options,
            )
        logger.info("Email sent successfully")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "Gmail authentication failed. Check GMAIL_FROM and GMAIL_APP_PASSWORD."
        )
        return False
    except Exception as exc:
        logger.error("Failed to send email: %s", exc)
        return False


def send_digest(listings: list[Listing]) -> bool:
    date_str = datetime.now().strftime("%A, %B %d, %Y")
    count = len(listings)
    city_formatted = TARGET_CITY.capitalize()
    if count > 0:
        subject = f"Apartment Hunter: {count} fresh apartment{'s' if count != 1 else ''} in {city_formatted} - {date_str}"
    else:
        subject = f"Apartment Hunter: no fresh opportunities today - {date_str}"
    return _send_email(
        subject,
        _build_daily_plain_text(listings, date_str),
        build_html_digest(listings, date_str),
    )


def send_weekly_digest(listings: list[Listing], week_label: str) -> bool:
    subject = f"Apartment Hunter Weekly: top 10 opportunities - {week_label}"
    return _send_email(
        subject,
        _build_weekly_plain_text(listings, week_label),
        build_weekly_html_digest(listings, week_label),
    )
