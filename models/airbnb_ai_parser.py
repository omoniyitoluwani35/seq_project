# -*- coding: utf-8 -*-
"""LLM fallback for Airbnb email extraction.

The regex extractors in airbnb_mail_parser.py handle the common case.
Where they return incomplete data - because Airbnb changed a template,
or the email is phrased unusually - this module asks a language model to
read the email instead.

Deliberate design points:
  - The model EXTRACTS a listing name as text; it never selects a unit.
    Unit matching stays a deterministic lookup in Python, because a
    hallucinated match would silently book the wrong room.
  - Every field the model returns is validated before use. Malformed
    JSON, wrong types and impossible dates are rejected rather than
    written.
  - The call is skipped entirely when regex extraction is complete, so
    the API is touched only on the exceptions.
"""
import json
import logging
import re
from datetime import date, datetime

import requests

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

SYSTEM_PROMPT = """You extract booking details from Airbnb notification emails.

Return ONLY a JSON object. No preamble, no markdown fences, no explanation.

Schema:
{
  "listing_name": string or null,   // the property/listing title exactly as written in the email
  "guest_name": string or null,     // the guest's name
  "checkin_date": "YYYY-MM-DD" or null,
  "checkout_date": "YYYY-MM-DD" or null,
  "confirmation_code": string or null,
  "gross_amount": number or null,   // total the guest paid
  "payout_amount": number or null,  // net amount Airbnb remits to the host
  "email_kind": one of "confirmation","request","cancellation","payout","date_change","unknown",
  "confidence": {                   // your confidence per field
    "listing_name": "high"|"low",
    "guest_name": "high"|"low",
    "dates": "high"|"low",
    "amounts": "high"|"low"
  }
}

Rules:
- Use null for anything not clearly stated. Never guess.
- Copy listing_name verbatim. Do not tidy, expand or abbreviate it.
- Amounts are numbers only, no currency symbols or thousands separators.
- If a value is implied but not stated, return null and mark confidence low.
"""


class AirbnbReservation(models.Model):
    _inherit = "airbnb.reservation"

    parsed_by_ai = fields.Boolean(
        readonly=True,
        copy=False,
        help="Set where regex extraction was incomplete and a language "
        "model was used to read the email instead.",
    )

    # ------------------------------------------------------- config

    @api.model
    def _ai_config(self):
        p = self.env["ir.config_parameter"].sudo()
        return {
            "enabled": p.get_param("airbnb.ai_fallback_enabled", "True") == "True",
            "key": p.get_param("airbnb.anthropic_api_key"),
            "model": p.get_param("airbnb.ai_model", "claude-3-5-haiku-20241022"),
        }

    # ---------------------------------------------------- the call

    @api.model
    def _ai_extract(self, subject, body):
        """Ask the model to read the email. Returns a validated dict, or
        False if the call failed or the response was unusable."""
        cfg = self._ai_config()
        if not cfg["enabled"]:
            return False
        if not cfg["key"]:
            _logger.warning(
                "Airbnb AI fallback needed but no API key configured "
                "(set airbnb.anthropic_api_key in system parameters)"
            )
            return False

        # Trim aggressively - the useful content is near the top and
        # long HTML footers waste tokens without adding information.
        content = f"Subject: {subject}\n\n{body[:6000]}"

        try:
            resp = requests.post(
                API_URL,
                headers={
                    "x-api-key": cfg["key"],
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": cfg["model"],
                    "max_tokens": 800,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": content}],
                },
                timeout=45,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as e:
            _logger.error("Airbnb AI extraction request failed: %s", e)
            return False

        try:
            text = "".join(
                block.get("text", "")
                for block in payload.get("content", [])
                if block.get("type") == "text"
            ).strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
            raw = json.loads(text)
        except (ValueError, AttributeError, TypeError) as e:
            _logger.error("Airbnb AI returned unparseable output: %s", e)
            return False

        return self._ai_validate(raw)

    # ------------------------------------------------- validation

    @api.model
    def _ai_validate(self, raw):
        """Coerce and sanity-check the model's output.

        Anything that fails validation becomes None rather than being
        written to a reservation. A model that returns a plausible but
        wrong date is worse than one that returns nothing.
        """
        if not isinstance(raw, dict):
            return False

        def as_text(v, limit=200):
            if not isinstance(v, str):
                return None
            v = v.strip()
            return v[:limit] if v else None

        def as_day(v):
            if not isinstance(v, str):
                return None
            try:
                d = datetime.strptime(v.strip(), "%Y-%m-%d").date()
            except ValueError:
                return None
            # Reject dates far outside any plausible booking window.
            if not (date(2020, 1, 1) <= d <= date(2100, 1, 1)):
                return None
            return d

        def as_money(v):
            if isinstance(v, (int, float)) and v >= 0:
                return float(v)
            if isinstance(v, str):
                try:
                    return float(re.sub(r"[^\d.]", "", v))
                except ValueError:
                    return None
            return None

        out = {
            "listing_name": as_text(raw.get("listing_name")),
            "guest_name": as_text(raw.get("guest_name"), 100),
            "checkin_date": as_day(raw.get("checkin_date")),
            "checkout_date": as_day(raw.get("checkout_date")),
            "confirmation_code": as_text(raw.get("confirmation_code"), 40),
            "gross_amount": as_money(raw.get("gross_amount")),
            "payout_amount": as_money(raw.get("payout_amount")),
            "email_kind": raw.get("email_kind")
                if raw.get("email_kind") in (
                    "confirmation", "request", "cancellation",
                    "payout", "date_change", "unknown")
                else "unknown",
            "confidence": raw.get("confidence") if isinstance(
                raw.get("confidence"), dict) else {},
        }

        # A checkout on or before checkin is nonsense - discard both
        # rather than write a reservation that fails the date constraint.
        if out["checkin_date"] and out["checkout_date"]:
            if out["checkout_date"] <= out["checkin_date"]:
                _logger.warning("AI returned checkout <= checkin - discarding dates")
                out["checkin_date"] = out["checkout_date"] = None

        # The confirmation code is the idempotency key, so it gets a
        # shape check regardless of what the model claims.
        code = out["confirmation_code"]
        if code and not re.fullmatch(r"[A-Z0-9]{6,20}", code):
            _logger.warning("AI returned implausible confirmation code %r", code)
            out["confirmation_code"] = None

        return out

    # ------------------------------------------- merge into regex result

    @api.model
    def _ai_fill_gaps(self, regex_result, subject, body):
        """Call the model only for what regex could not read, and merge.

        regex_result is a dict with the same keys. Regex values win where
        present: they are deterministic, so a match is more trustworthy
        than a model's reading.
        """
        missing = [k for k in ("checkin_date", "checkout_date",
                               "confirmation_code", "guest_name",
                               "gross_amount", "unit")
                   if not regex_result.get(k)]
        if not missing:
            return regex_result, False

        _logger.info("Airbnb regex incomplete (%s) - calling AI fallback",
                     ", ".join(missing))

        ai = self._ai_extract(subject, body)
        if not ai:
            return regex_result, False

        merged = dict(regex_result)
        for key in ("checkin_date", "checkout_date", "confirmation_code",
                    "guest_name", "gross_amount", "payout_amount"):
            if not merged.get(key) and ai.get(key):
                merged[key] = ai[key]

        # Unit stays a Python lookup against the extracted listing name.
        if not merged.get("unit") and ai.get("listing_name"):
            merged["unit"] = self._airbnb_match_unit(ai["listing_name"], "")

        if not merged.get("kind") and ai.get("email_kind") != "unknown":
            merged["kind"] = ai["email_kind"]

        merged["_ai_confidence"] = ai.get("confidence", {})
        return merged, True