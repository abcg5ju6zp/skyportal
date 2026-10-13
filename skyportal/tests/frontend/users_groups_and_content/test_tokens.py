import re
import uuid

import pytest
from playwright.sync_api import expect

from skyportal.tests import open_preferences_panel


def _fill_token_form(page, name, group_name, *, close_secret=True, success=True):
    page.locator('//input[@name="name"]').first.fill(name)
    group_input = page.get_by_test_id("token-groups").locator("input").first
    group_input.click()
    page.locator('//li[contains(.,"' + group_name + '")]').first.click()
    # disableCloseOnSelect keeps the option popper open; dismiss it so it does
    # not intercept the ACL checkbox click below.
    page.keyboard.press("Escape")
    page.locator('//*[@data-testid="acls[0]"]').first.click()
    page.locator('//button[contains(.,"Generate Token")]').first.click()
    if not success:
        return
    expect(page.get_by_test_id("new-token-secret").first).to_be_visible()
    if close_secret:
        page.locator('//button[contains(.,"Done")]').first.click()


@pytest.mark.flaky(reruns=2)
def test_add_token(page, user, public_group):
    token_name = str(uuid.uuid4())
    page.goto(f"/become_user/{user.id}")
    page.goto("/profile")
    open_preferences_panel(page, "api-tokens")
    _fill_token_form(page, token_name, public_group.name)
    expect(
        page.locator(f'//div[@role="gridcell" and contains(.,"{token_name}")]').first
    ).to_be_visible()
    # the full secret is never rendered in the list
    expect(page.locator('//div[contains(.,"Fingerprint")]').first).to_be_visible()


@pytest.mark.flaky(reruns=2)
def test_multiple_scoped_tokens_allowed(page, user, public_group):
    # scoped tokens are not subject to the legacy one-token-per-user cap
    page.goto(f"/become_user/{user.id}")
    page.goto("/profile")
    open_preferences_panel(page, "api-tokens")
    name1 = str(uuid.uuid4())
    _fill_token_form(page, name1, public_group.name)
    name2 = str(uuid.uuid4())
    _fill_token_form(page, name2, public_group.name)
    expect(
        page.locator(f'//div[@role="gridcell" and contains(.,"{name2}")]').first
    ).to_be_visible()


@pytest.mark.flaky(reruns=2)
def test_rotate_and_revoke_scoped_token(page, user, public_group):
    token_name = str(uuid.uuid4())
    page.goto(f"/become_user/{user.id}")
    page.goto("/profile")
    open_preferences_panel(page, "api-tokens")
    _fill_token_form(page, token_name, public_group.name)

    # only the latest active generation offers rotation
    rows = page.locator(".MuiDataGrid-row", has_text=token_name)
    expect(rows).to_have_count(1)
    rows.first.locator('[data-testid^="rotate-"]').click()

    dialog = page.get_by_role("dialog")
    dialog.locator("input[type=number]").fill("60")
    dialog.get_by_test_id("confirm-rotate").click()
    expect(page.get_by_test_id("rotated-token-secret")).to_be_visible()
    page.locator('//button[contains(.,"Done")]').first.click()

    # same series, two generations: old one overlapping, new one active
    expect(rows).to_have_count(2)
    gen1 = rows.filter(has=page.get_by_text(re.compile(r"gen 1\s*$")))
    gen2 = rows.filter(has=page.get_by_text(re.compile(r"gen 2\s*$")))
    expect(
        gen1.locator(".MuiChip-root").filter(has_text="overlapping")
    ).to_be_visible()
    expect(
        gen2.locator(".MuiChip-root").filter(has_text="active")
    ).to_be_visible()

    # revoke the new generation: status flips to revoked
    gen2.locator('[data-testid^="revoke-"]').first.click()
    expect(
        gen2.locator(".MuiChip-root").filter(has_text="revoked")
    ).to_be_visible()


@pytest.mark.flaky(reruns=2)
def test_delete_token(page, user, view_only_token):
    page.goto(f"/become_user/{user.id}")
    page.goto("/profile")
    open_preferences_panel(page, "api-tokens")
    # only the fingerprint (first 8 chars) is shown, never the full secret
    cell = page.locator(
        f'//div[@role="gridcell" and contains(.,"{view_only_token[:8]}")]'
    ).first
    expect(cell).to_be_visible()
    page.get_by_label("Delete token").first.click()
    expect(cell).to_be_hidden()


@pytest.mark.flaky(reruns=2)
def test_add_duplicate_token_error_message(page, super_admin_user, public_group):
    token_name = str(uuid.uuid4())
    page.goto(f"/become_user/{super_admin_user.id}")
    page.goto("/profile")
    open_preferences_panel(page, "api-tokens")
    _fill_token_form(page, token_name, public_group.name)

    # a second, otherwise-valid creation with the same name is rejected
    _fill_token_form(page, token_name, public_group.name, success=False)
    expect(
        page.locator('//*[contains(text(),"Duplicate token name")]').first
    ).to_be_visible()


@pytest.mark.flaky(reruns=2)
def test_sys_admin_can_create_multiple_tokens(
    page, super_admin_user, public_group
):
    page.goto(f"/become_user/{super_admin_user.id}")
    page.goto("/profile")
    open_preferences_panel(page, "api-tokens")
    token1_name = str(uuid.uuid4())
    _fill_token_form(page, token1_name, public_group.name)
    expect(
        page.locator(
            f'//div[@role="gridcell" and contains(.,"{token1_name}")]'
        ).first
    ).to_be_visible()

    token2_name = str(uuid.uuid4())
    _fill_token_form(page, token2_name, public_group.name)
    expect(
        page.locator(
            f'//div[@role="gridcell" and contains(.,"{token2_name}")]'
        ).first
    ).to_be_visible()
