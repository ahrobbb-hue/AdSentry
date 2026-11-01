from main import register_persistent_views


class RecordingBot:
    def __init__(self) -> None:
        self.views = []

    def add_view(self, view) -> None:
        self.views.append(view)


def test_persistent_views_register_each_active_post_once() -> None:
    bot = RecordingBot()

    register_persistent_views(bot, [12, 34, 12])

    custom_ids = {
        item.custom_id
        for view in bot.views
        for item in view.children
        if item.custom_id is not None
    }
    assert custom_ids == {
        "adsentry:interest:0",
        "adsentry:verify:0",
        "adsentry:translate:0",
        "adsentry:interest:12",
        "adsentry:verify:12",
        "adsentry:interest:34",
        "adsentry:verify:34",
    }
    assert all(view.timeout is None for view in bot.views)
