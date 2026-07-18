from importlib import import_module


def test_lark_module_owns_transport_and_rendering() -> None:
    client = import_module("lark_bot.modules.lark.lark_client")
    connection = import_module("lark_bot.modules.lark.lark_connection")
    router = import_module("lark_bot.modules.lark.lark_router")
    render = import_module("lark_bot.modules.lark.lark_render")

    assert client.LarkBotClient
    assert connection.LarkLongConnection
    assert router.LarkControlRouter
    assert render.render_outbox_notification
