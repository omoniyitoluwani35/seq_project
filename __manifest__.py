{
    "name": "Airbnb Booking Sync",
    "version": "0.1",
    "summary": "Airbnb email-parsed bookings, unit calendars, and two-way iCal sync",
    "description": """
Airbnb Booking Sync
====================
Core data model and reservation state machine for syncing a single Airbnb
host account (one building, multiple units/listings) into Odoo.

This module covers:
- Property (building) and Unit (individual Airbnb listing) records
- Reservation model with a Pending/Confirmed/Cancelled/Done state machine
- Separate tracking of gross booking value vs. actual Airbnb payout
- Overlap protection so two confirmed reservations can never share a unit
- Idempotency guard on Airbnb confirmation codes (prevents duplicate
  sale orders from repeat/duplicate confirmation emails)

Gmail parsing, iCal export/import, and monitoring are built as separate
follow-on pieces that plug into this data model.
    """,
    "author": "NIVIC Technology ",
    "category": "Sales/Sales",
    "depends": ["sale_management", "contacts", "analytic", "hr", "stock", "project"],
    "external_dependencies": {"python": ["icalendar"]},
    "data": [
        "security/airbnb_security_policy.xml",
        "security/ir.model.access.csv",
        "data/ical_cron.xml",
        "wizard/airbnb_maintanance_wizard.xml",
        "views/airbnb_views.xml",
        "views/gantt_view.xml",
        "views/project_task.xml",
        
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
