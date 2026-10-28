from app.bot.ui import Flow, MAIN


def test_phone_login_has_number_code_and_two_factor_states() -> None:
    assert Flow.phone_number.state.endswith(":phone_number")
    assert Flow.phone_code.state.endswith(":phone_code")
    assert Flow.phone_password.state.endswith(":phone_password")


def test_main_menu_has_settings_and_no_help_button() -> None:
    callback_data = {
        button.callback_data
        for row in MAIN.inline_keyboard
        for button in row
    }
    assert "nav:settings" in callback_data
    assert "nav:help" not in callback_data
