from flask import Blueprint, render_template, jsonify

from app.data import DEVICES
from app.services.reconciler import reconcile_all

breakdown_bp = Blueprint("breakdown", __name__)


@breakdown_bp.route("/")
def index():
    return breakdown()


@breakdown_bp.route("/breakdown")
def breakdown():
    companies = reconcile_all(DEVICES)
    total_matched = sum(c["matched_count"] for c in companies)
    total_mismatched = sum(c["mismatched_count"] for c in companies)
    return render_template(
        "breakdown.html",
        companies=companies,
        total_matched=total_matched,
        total_mismatched=total_mismatched,
    )


@breakdown_bp.route("/api/breakdown")
def api_breakdown():
    """JSON endpoint – useful for testing and future front-end consumption."""
    companies = reconcile_all(DEVICES)
    return jsonify(companies)
