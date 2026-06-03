"""Email digest builder and sender for house-for-sale listings."""

from __future__ import annotations

import html
import logging
import os
import smtplib
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTPUTF8

_NL_DAYS = ["maandag","dinsdag","woensdag","donderdag","vrijdag","zaterdag","zondag"]
_NL_MONTHS = ["","januari","februari","maart","april","mei","juni","juli","augustus","september","oktober","november","december"]

def _format_nl_date(dt: datetime) -> str:
    """Format date to 'dinsdag 02 juni 2026'"""
    day = _NL_DAYS[dt.weekday()]
    month = _NL_MONTHS[dt.month]
    return f"{day} {dt.day:02d} {month} {dt.year}"

from config import EXCLUDE_CITIES_FINAL, MIN_BEDROOMS, MIN_LIVING_SURFACE, MIN_LOT_SURFACE, MIN_PRICE, MAX_PRICE
from scrapers.base import Listing

logger = logging.getLogger(__name__)
MAILER_VERSION = "2026-06-01-1"


def _clean_text(value: str) -> str:
    return value.replace("\xa0", " ").replace("\u202f", " ").replace("\r\n", "\n")


def _clean_email_field(value: str) -> str:
    return _clean_text(value).strip()


def _ascii_safe(value: str) -> str:
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
    colors = {"immoweb": "#FF6B00", "zimmo": "#2563EB", "immoscoop": "#7C3AED"}
    color = colors.get(platform, "#6B7280")
    return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
            f'border-radius:4px;font-size:11px;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:0.5px">{html.escape(platform)}</span>')


def _status_badge(status: str | None) -> str:
    if not status:
        return ""
    badges = {
        "under_option": ("⚖️ Onder optie", "#D97706"),
        "life_annuity": ("🔄 Lijfrente", "#DC2626"),
        "public_sale": ("🔨 Openbare verkoop", "#7C3AED"),
    }
    label, color = badges.get(status, (status.replace("_", " ").title(), "#6B7280"))
    return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
            f'border-radius:4px;font-size:11px;font-weight:600;'
            f'margin-left:4px">{html.escape(label)}</span>')


def _format_price(price: int) -> str:
    """Format price like €450.000."""
    if price >= 1_000_000:
        return f"€{price/1_000_000:.1f}M"
    return f"€{price:,}".replace(",", ".")


def _format_m2(val: int | None) -> str:
    if val is None:
        return "?"
    return f"{val}m²"


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
            if image_url and (image_url.startswith("http") or image_url.startswith("data:")):
                thumbnail_html = (
                    f'<img src="{html.escape(image_url)}" alt="Listing photo" '
                    f'style="width:100%;max-width:180px;height:100px;'
                    f'object-fit:cover;border-radius:6px;margin-bottom:6px" />'
                )

        details = []
        if listing.bedrooms:
            details.append(f"🛏️ {listing.bedrooms}slaapk")
        if listing.surface_m2:
            details.append(f"📐 {_format_m2(listing.surface_m2)}")
        if listing.lot_surface_m2:
            details.append(f"🌳 {_format_m2(listing.lot_surface_m2)}")
        if listing.epc_label:
            details.append(f"⚡ EPC {listing.epc_label}")
        details_html = " &middot; ".join(html.escape(_ascii_safe(p)) for p in details) if details else ""

        score_breakdown = ""  # removed — final score is enough

        reasoning_html = ""
        if reasoning and reasoning != "AI scoring unavailable — unranked":
            short_r = reasoning[:100] + ("..." if len(reasoning) > 100 else "")
            reasoning_html = (f'<p style="margin:4px 0;font-size:11px;color:#6B7280;'
                              f'font-style:italic;line-height:1.3">'
                              f'{html.escape(_ascii_safe(short_r))}</p>')

        cards_html += f"""
        <div style="background:#fff;border-radius:8px;padding:12px;
                    margin-bottom:8px;border:1px solid #E5E7EB;">
            <div style="display:flex;gap:10px;flex-wrap:wrap">
                <div style="flex-shrink:0">{thumbnail_html}</div>
                <div style="flex:1;min-width:180px">
                    <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;flex-wrap:wrap">
                        <span style="background:{score_color};color:#fff;padding:2px 8px;
                                    border-radius:12px;font-size:12px;font-weight:700">
                            {score_emoji} {score_display}</span>
                        {_platform_badge(listing.platform)}
                        {_status_badge(listing.status)}
                    </div>
                    <p style="margin:0 0 3px;font-size:15px;font-weight:700;color:#059669;line-height:1.3">
                        {_format_price(listing.price)} &middot; {html.escape(_ascii_safe(title))}</p>
                    <p style="margin:0 0 3px;font-size:12px;color:#6B7280">
                        {html.escape(_ascii_safe(address))}</p>
                    <p style="margin:0 0 2px;font-size:12px;color:#374151">{details_html}</p>
                    {reasoning_html}
                    <a href="{html.escape(listing.url)}" target="_blank" rel="noopener"
                       style="display:inline-block;margin-top:4px;padding:5px 14px;
                              background:#2563EB;color:#fff;text-decoration:none;
                              border-radius:5px;font-size:12px;font-weight:600">Bekijk &#8599;</a>
                </div>
            </div>
        </div>"""

    return cards_html


def _build_shell(heading: str, subtitle: str, body_html: str, footer_text: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="nl">
    <head><meta charset="utf-8"/></head>
    <body style="margin:0;padding:0;background:#F9FAFB;font-family:-apple-system,sans-serif">
        <div style="max-width:580px;margin:0 auto;padding:16px">
            <div style="background:#1E40AF;border-radius:12px;padding:16px;margin-bottom:16px;text-align:center">
                <h1 style="margin:0;font-size:20px;color:#fff">{html.escape(heading)}</h1>
                <p style="margin:4px 0 0;color:rgba(255,255,255,0.85);font-size:13px">{html.escape(subtitle)}</p>
            </div>
            {body_html}
            <div style="text-align:center;padding:12px;color:#9CA3AF;font-size:11px">
                <p>{html.escape(footer_text)}</p>
            </div>
        </div>
    </body>
    </html>"""


def build_html_digest(listings: list[Listing], date_str: str) -> str:
    listings = sorted(listings, key=lambda l: l.final_score or 0, reverse=True)
    count = len(listings)
    max_show = 15
    shown = listings[:max_show]
    footer = f"Huizen te koop | €{MIN_PRICE:,}-{MAX_PRICE:,} | min {MIN_BEDROOMS} slaapk | min {MIN_LIVING_SURFACE}m² | EPC A-C"

    if count == 0:
        body_html = """
        <div style="background:#fff;border-radius:12px;padding:32px;border:1px solid #E5E7EB;text-align:center">
            <p style="font-size:40px;margin:0">😴</p>
            <h2 style="margin:12px 0 8px;color:#374151;font-size:18px">Rustige dag op de markt</h2>
            <p style="color:#6B7280;font-size:14px;margin:0">Geen nieuwe huizen gevonden vandaag. Geduld — de juiste komt vanzelf.</p>
        </div>"""
        return _build_shell("Huizenjacht", f"{date_str} - geen nieuwe kansen vandaag", body_html, footer)

    price_min = min(l.price for l in listings)
    price_max = max(l.price for l in listings)
    top_score = f"{listings[0].final_score:.1f}" if listings[0].final_score is not None else "-"

    more_notice = f"<p style='font-size:12px;color:#9CA3AF;text-align:center;'>Nog {count - max_show} meer...</p>" if count > max_show else ""
    body_html = f"""
    <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
        <div style="flex:1;min-width:80px;background:#fff;border-radius:8px;padding:10px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:22px;font-weight:700;color:#2563EB">{count}</div>
            <div style="font-size:11px;color:#6B7280;margin-top:2px">Nieuw</div>
        </div>
        <div style="flex:1;min-width:80px;background:#fff;border-radius:8px;padding:10px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:16px;font-weight:700;color:#059669">{_format_price(price_min)}-{_format_price(price_max)}</div>
            <div style="font-size:11px;color:#6B7280;margin-top:2px">Prijs</div>
        </div>
        <div style="flex:1;min-width:80px;background:#fff;border-radius:8px;padding:10px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:22px;font-weight:700;color:#7C3AED">{top_score}</div>
            <div style="font-size:11px;color:#6B7280;margin-top:2px">Top</div>
        </div>
    </div>
    {_build_listing_cards(shown)}
    {more_notice}"""

    return _build_shell("Huizenjacht", f"{date_str} - {count} nieuwe{' huizen' if count != 1 else ' huis'} gevonden", body_html, footer)


def _build_daily_plain_text(listings: list[Listing], date_str: str) -> str:
    lines = [f"Huizenjacht - {date_str}", ""]
    if listings:
        lines.append(f"{len(listings)} nieuw(e) huis/huizen gevonden:")
        lines.append("")
        for i, l in enumerate(listings, 1):
            score = f"{l.final_score:.1f}" if l.final_score is not None else "-"
            status = ""
            if l.status == "under_option":
                status = " [⚖️ ONDER OPTIE]"
            elif l.status == "life_annuity":
                status = " [🔄 LIJFRENTE]"
            lines.append(f"#{i} [{score}/10]{status} {_ascii_safe(l.title)}")
            lines.append(f"    {_format_price(l.price)} - {_ascii_safe(l.address)}")
            lines.append(f"    {l.url}")
            lines.append("")
    else:
        lines.append("Rustige dag. Geen nieuwe huizen.")
    return "\n".join(lines)


def _smtp_config() -> tuple:
    return (
        _clean_email_field(os.environ.get("SMTP_FROM", os.environ.get("GMAIL_FROM", ""))),
        os.environ.get("SMTP_PASSWORD", os.environ.get("GMAIL_APP_PASSWORD", "")),
        _clean_email_field(os.environ.get("EMAIL_TO", "")),
        _clean_email_field(os.environ.get("EMAIL_CC", "")),
    )


def _send_email(subject_text: str, plain_text: str, html_body: str) -> bool:
    smtp_from, smtp_password, email_to, email_cc = _smtp_config()
    smtp_host = os.environ.get("SMTP_HOST", "ssl0.ovh.net")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_use_tls = os.environ.get("SMTP_TLS", "0") == "1"

    if not all([smtp_from, smtp_password, email_to]):
        logger.error("Email config incomplete. Need SMTP_FROM, SMTP_PASSWORD, EMAIL_TO")
        return False

    msg = EmailMessage(policy=SMTPUTF8)
    msg["Subject"] = subject_text
    msg["From"] = smtp_from
    msg["To"] = email_to
    if email_cc:
        msg["Cc"] = email_cc

    msg.set_content(_ascii_safe(plain_text), charset="utf-8")
    msg.add_alternative(_ascii_safe(html_body), subtype="html", charset="utf-8")

    recipients = [email_to]
    if email_cc:
        recipients.extend([a.strip() for a in email_cc.split(",") if a.strip()])

    try:
        logger.info("Sending via %s:%s to %s (CC: %s)", smtp_host, smtp_port, email_to, email_cc or "none")
        if smtp_use_tls:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_from, smtp_password)
                mo = ["SMTPUTF8"] if server.has_extn("smtputf8") else []
                server.sendmail(smtp_from, recipients, msg.as_bytes(policy=SMTPUTF8), mail_options=mo)
        else:
            import ssl
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
                server.login(smtp_from, smtp_password)
                mo = ["SMTPUTF8"] if server.has_extn("smtputf8") else []
                server.sendmail(smtp_from, recipients, msg.as_bytes(policy=SMTPUTF8), mail_options=mo)
        logger.info("Email sent")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP auth failed. Check credentials.")
        return False
    except Exception as exc:
        logger.error("Failed to send: %s", exc)
        return False


def send_digest(listings: list[Listing]) -> bool:
    date_str = _format_nl_date(datetime.now())
    count = len(listings)
    if count > 0:
        subject = f"Huizenjacht: {count} nieuw{' huis' if count==1 else 'e huizen'} - {date_str}"
    else:
        subject = f"Huizenjacht: niks nieuws - {date_str}"
    return _send_email(subject, _build_daily_plain_text(listings, date_str), build_html_digest(listings, date_str))


def send_weekly_digest(listings: list[Listing], week_label: str) -> bool:
    subject = f"Huizenjacht Weekoverzicht: top kansen - {week_label}"
    return _send_email(subject, f"Weekoverzicht {week_label}", build_html_digest(listings, week_label))
