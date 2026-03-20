from flask import Blueprint, render_template
from flask_login import login_required

help_bp = Blueprint("help", __name__)


@help_bp.route("/help")
@login_required
def index():
    return render_template("help/index.html")
