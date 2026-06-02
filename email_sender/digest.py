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
                    f'style="width:100%;max-width:280px;height:180px;'
                    f'object-fit:cover;border-radius:8px;margin-bottom:12px" />'
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

        score_parts = []
        if listing.text_score is not None:
            score_parts.append(f"Tekst: {listing.text_score:.1f}")
        if listing.photo_score is not None:
            score_parts.append(f"Foto: {listing.photo_score:.1f}")
        score_breakdown = html.escape(_ascii_safe(" | ".join(score_parts))) if score_parts else ""

        reasoning_html = ""
        if reasoning and reasoning != "AI scoring unavailable — unranked":
            reasoning_html = (f'<p style="margin:8px 0;font-size:13px;color:#6B7280;'
                              f'font-style:italic;line-height:1.4">'
                              f'{html.escape(_ascii_safe(reasoning))}</p>')

        cards_html += f"""
        <div style="background:#fff;border-radius:12px;padding:20px;
                    margin-bottom:16px;border:1px solid #E5E7EB;
                    box-shadow:0 1px 3px rgba(0,0,0,0.06)">
            <div style="display:flex;gap:16px;flex-wrap:wrap">
                <div style="flex-shrink:0">{thumbnail_html}</div>
                <div style="flex:1;min-width:200px">
                    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
                        <span style="background:#F3F4F6;color:#374151;padding:2px 10px;
                                    border-radius:20px;font-size:13px;font-weight:700">#{index}</span>
                        <span style="background:{score_color};color:#fff;padding:4px 12px;
                                    border-radius:20px;font-size:14px;font-weight:700">
                            {score_emoji} {score_display}/10</span>
                        {_platform_badge(listing.platform)}
                    </div>
                    <h3 style="margin:0 0 4px;font-size:16px;color:#111827;font-weight:600;line-height:1.3">
                        {html.escape(_ascii_safe(title))}</h3>
                    <p style="margin:0 0 6px;font-size:18px;font-weight:700;color:#059669">
                        {_format_price(listing.price)}</p>
                    <p style="margin:0 0 6px;font-size:13px;color:#6B7280">
                        {html.escape(_ascii_safe(address))}</p>
                    <p style="margin:0 0 4px;font-size:13px;color:#374151">{details_html}</p>
                    <p style="margin:4px 0;font-size:11px;color:#9CA3AF">{score_breakdown}</p>
                    {reasoning_html}
                    <a href="{html.escape(listing.url)}" target="_blank" rel="noopener"
                       style="display:inline-block;margin-top:8px;padding:8px 20px;
                              background:#2563EB;color:#fff;text-decoration:none;
                              border-radius:6px;font-size:13px;font-weight:600">Bekijk</a>
                </div>
            </div>
        </div>"""

    return cards_html


def _build_shell(heading: str, subtitle: str, body_html: str, footer_text: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="nl">
    <head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
    <body style="margin:0;padding:0;background:#F9FAFB;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
        <div style="max-width:640px;margin:0 auto;padding:24px 16px">
            <div style="background:linear-gradient(135deg,#1E40AF 0%,#7C3AED 100%);
                        border-radius:16px;padding:28px 24px;margin-bottom:24px;text-align:center">
                <h1 style="margin:0;font-size:24px;color:#fff;font-weight:700">{html.escape(heading)}</h1>
                <p style="margin:8px 0 0;color:rgba(255,255,255,0.85);font-size:14px">{html.escape(subtitle)}</p>
            </div>
            {body_html}
            <div style="text-align:center;padding:20px;color:#9CA3AF;font-size:12px">
                <p>{html.escape(footer_text)}</p>
            </div>
        </div>
    </body>
    </html>"""


def build_html_digest(listings: list[Listing], date_str: str) -> str:
    count = len(listings)
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

    body_html = f"""
    <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap">
        <div style="flex:1;min-width:120px;background:#fff;border-radius:10px;padding:16px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:28px;font-weight:700;color:#2563EB">{count}</div>
            <div style="font-size:12px;color:#6B7280;margin-top:4px">Nieuw</div>
        </div>
        <div style="flex:1;min-width:120px;background:#fff;border-radius:10px;padding:16px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:28px;font-weight:700;color:#059669">{_format_price(price_min)}-{_format_price(price_max)}</div>
            <div style="font-size:12px;color:#6B7280;margin-top:4px">Prijsrange</div>
        </div>
        <div style="flex:1;min-width:120px;background:#fff;border-radius:10px;padding:16px;text-align:center;border:1px solid #E5E7EB">
            <div style="font-size:28px;font-weight:700;color:#7C3AED">{top_score}</div>
            <div style="font-size:12px;color:#6B7280;margin-top:4px">Top score</div>
        </div>
    </div>
    {_build_listing_cards(listings)}
    <p style="font-size:12px;color:#9CA3AF;text-align:center;margin-top:8px">
        Gezocht: Geel, Lier, Ranst, Broechem, Emblem, Vremde, Wommelgem, Kessel · 
        Uitgesloten: Koningshooikt, Oelegem, Nijlen, Bevel · 
        Geen renovatie
    </p>"""

    return _build_shell("Huizenjacht", f"{date_str} - {count} nieuwe{' huizen' if count != 1 else ' huis'} gevonden", body_html, footer)


def _build_daily_plain_text(listings: list[Listing], date_str: str) -> str:
    lines = [f"Huizenjacht - {date_str}", ""]
    if listings:
        lines.append(f"{len(listings)} nieuw(e) huis/huizen gevonden:")
        lines.append("")
        for i, l in enumerate(listings, 1):
            score = f"{l.final_score:.1f}" if l.final_score is not None else "-"
            lines.append(f"#{i} [{score}/10] {_ascii_safe(l.title)}")
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
