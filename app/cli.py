"""Command line: flask --app wsgi seed | create-admin | expire-orders"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

import click
from flask import current_app

from .extensions import db
from .models import AdminUser, Book

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


def seed_catalog(force: bool = False) -> int:
    from seed.catalog import seed_all

    if Book.query.count() and not force:
        return 0
    return seed_all()


def register_cli(app):
    @app.cli.command("seed")
    @click.option("--force", is_flag=True, help="Add missing seed content even if the site already has books.")
    def seed_cmd(force):
        """Create the categories, the first book, poems and pages from seed/ (safe to run twice)."""
        n = seed_catalog(force)
        click.echo(f"{n} seed items created (book, poems, page, banner).")

    @app.cli.command("create-admin")
    @click.argument("username")
    def create_admin(username):
        """Create an admin, or reset their password. Reads ADMIN_PASSWORD or prompts."""
        password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password (min 10 chars): ")
        if len(password) < 10:
            raise click.ClickException("Password must be at least 10 characters.")
        user = AdminUser.query.filter_by(username=username).first() or AdminUser(username=username)
        user.set_password(password)
        user.is_active = True
        user.failed_logins = 0
        user.locked_until = None
        db.session.add(user)
        db.session.commit()
        click.echo(f"Admin '{username}' is ready.")

    @app.cli.command("expire-orders")
    def expire_orders():
        """Cancel unpaid card-to-card orders older than ORDER_HOLD_HOURS (run from cron)."""
        from .orders import expire_unpaid
        click.echo(f"{expire_unpaid(current_app.config['ORDER_HOLD_HOURS'])} orders cancelled.")
