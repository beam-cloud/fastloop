"""
Tests for the Slack integration.

Verifies:
1. SlackIntegration initialization with setup callback
2. setup_for_context creates and stores client
3. get_client_for_context retrieves the client
4. emit sends messages using the context's client
5. Events are registered correctly
6. wait_for works with Slack events
7. Thread routing with root_ts mapping
8. Subtype filtering (bot_message, message_changed, etc.)
9. Inbound files/links/blocks/attachments parsing
10. Outbound file uploads
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastloop import FastLoop, LoopContext
from fastloop.integrations.slack import (
    IGNORED_MESSAGE_SUBTYPES,
    SlackAppMentionEvent,
    SlackFile,
    SlackFileSharedEvent,
    SlackFileUploadEvent,
    SlackIntegration,
    SlackLinkSharedEvent,
    SlackMessageEvent,
    SlackReactionEvent,
    _extract_urls,
    _parse_slack_files,
)
from fastloop.types import IntegrationType


class TestSlackIntegrationInit:
    def test_requires_signing_secret(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret123", setup=setup)
        assert integration._signing_secret == "secret123"
        assert integration._setup_callback == setup

    def test_creates_verifier(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret123", setup=setup)
        assert integration.verifier is not None

    def test_type_returns_slack(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret123", setup=setup)
        assert integration.type() == IntegrationType.SLACK


class TestSlackIntegrationSetup:
    @pytest.mark.asyncio
    async def test_setup_for_context_calls_callback(self):
        callback_called = False
        received_context = None
        received_event = None

        async def setup(ctx, event):
            nonlocal callback_called, received_context, received_event
            callback_called = True
            received_context = ctx
            received_event = event
            return "xoxb-test-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_context = MagicMock(spec=LoopContext)
        mock_context.set_integration_client = MagicMock()
        mock_context.get_integration_client = MagicMock(return_value=None)

        mock_event = SlackAppMentionEvent(
            channel="C123",
            user="U456",
            text="hello",
            ts="123.456",
            team="T789",
            event_ts="123.456",
        )

        with patch("fastloop.integrations.slack.AsyncWebClient") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_cls.return_value = mock_client_instance

            await integration.setup_for_context(mock_context, mock_event)

            assert callback_called
            assert received_context == mock_context
            assert received_event == mock_event
            mock_client_cls.assert_called_once_with(token="xoxb-test-token")
            mock_context.set_integration_client.assert_called_once_with(
                IntegrationType.SLACK, mock_client_instance
            )

    @pytest.mark.asyncio
    async def test_get_client_for_context_returns_stored_client(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_client = MagicMock()
        mock_context = MagicMock(spec=LoopContext)
        mock_context.get_integration_client = MagicMock(return_value=mock_client)

        result = integration.get_client_for_context(mock_context)

        assert result == mock_client
        mock_context.get_integration_client.assert_called_once_with(
            IntegrationType.SLACK
        )

    @pytest.mark.asyncio
    async def test_get_client_for_context_raises_if_not_initialized(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_context = MagicMock(spec=LoopContext)
        mock_context.get_integration_client = MagicMock(return_value=None)

        with pytest.raises(ValueError, match="Slack client not initialized"):
            integration.get_client_for_context(mock_context)


class TestSlackIntegrationEmit:
    @pytest.mark.asyncio
    async def test_emit_requires_context(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="hello",
            ts="123.456",
            team="T789",
            event_ts="123.456",
        )

        with pytest.raises(ValueError, match="Context is required"):
            await integration.emit(event, context=None)

    @pytest.mark.asyncio
    async def test_emit_message_calls_chat_post_message(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_client = AsyncMock()
        mock_context = MagicMock(spec=LoopContext)
        mock_context.get_integration_client = MagicMock(return_value=mock_client)

        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="hello world",
            ts="123.456",
            thread_ts="123.456",
            team="T789",
            event_ts="123.456",
        )

        await integration.emit(event, context=mock_context)

        mock_client.chat_postMessage.assert_called_once_with(
            channel="C123", text="hello world", thread_ts="123.456"
        )

    @pytest.mark.asyncio
    async def test_emit_app_mention_calls_chat_post_message(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_client = AsyncMock()
        mock_context = MagicMock(spec=LoopContext)
        mock_context.get_integration_client = MagicMock(return_value=mock_client)

        event = SlackAppMentionEvent(
            channel="C123",
            user="U456",
            text="<@BOT> hello",
            ts="123.456",
            thread_ts="123.456",
            team="T789",
            event_ts="123.456",
        )

        await integration.emit(event, context=mock_context)

        mock_client.chat_postMessage.assert_called_once_with(
            channel="C123", text="<@BOT> hello", thread_ts="123.456"
        )

    @pytest.mark.asyncio
    async def test_emit_reaction_calls_reactions_add(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_client = AsyncMock()
        mock_context = MagicMock(spec=LoopContext)
        mock_context.get_integration_client = MagicMock(return_value=mock_client)

        event = SlackReactionEvent(
            channel="C123",
            user="U456",
            reaction="thumbsup",
            item_user="U789",
            item={"type": "message", "ts": "123.456"},
            event_ts="123.456",
        )

        await integration.emit(event, context=mock_context)

        mock_client.reactions_add.assert_called_once_with(
            channel="C123",
            name="thumbsup",
            timestamp="123.456",
        )

    @pytest.mark.asyncio
    async def test_emit_message_with_blocks_and_attachments(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_client = AsyncMock()
        mock_context = MagicMock(spec=LoopContext)
        mock_context.get_integration_client = MagicMock(return_value=mock_client)

        blocks = [{"type": "section", "text": {"type": "plain_text", "text": "Hello"}}]
        attachments = [{"color": "#36a64f", "text": "Attachment text"}]

        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="hello with blocks",
            ts="123.456",
            thread_ts="123.456",
            team="T789",
            event_ts="123.456",
            blocks=blocks,
            attachments=attachments,
        )

        await integration.emit(event, context=mock_context)

        mock_client.chat_postMessage.assert_called_once_with(
            channel="C123",
            text="hello with blocks",
            thread_ts="123.456",
            blocks=blocks,
            attachments=attachments,
        )

    @pytest.mark.asyncio
    async def test_emit_file_upload_with_content(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_client = AsyncMock()
        mock_context = MagicMock(spec=LoopContext)
        mock_context.get_integration_client = MagicMock(return_value=mock_client)

        event = SlackFileUploadEvent(
            channel="C123",
            thread_ts="123.456",
            filename="test.txt",
            content=b"Hello, World!",
            title="Test File",
            initial_comment="Here's a file",
        )

        await integration.emit(event, context=mock_context)

        mock_client.files_upload_v2.assert_called_once_with(
            channels="C123",
            filename="test.txt",
            thread_ts="123.456",
            title="Test File",
            initial_comment="Here's a file",
            content=b"Hello, World!",
        )

    @pytest.mark.asyncio
    async def test_emit_file_upload_with_file_path(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_client = AsyncMock()
        mock_context = MagicMock(spec=LoopContext)
        mock_context.get_integration_client = MagicMock(return_value=mock_client)

        event = SlackFileUploadEvent(
            channel="C123",
            filename="report.pdf",
            file_path="/tmp/report.pdf",
        )

        await integration.emit(event, context=mock_context)

        mock_client.files_upload_v2.assert_called_once_with(
            channels="C123",
            filename="report.pdf",
            file="/tmp/report.pdf",
        )

    @pytest.mark.asyncio
    async def test_emit_file_upload_requires_content_or_path(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)

        mock_client = AsyncMock()
        mock_context = MagicMock(spec=LoopContext)
        mock_context.get_integration_client = MagicMock(return_value=mock_client)

        event = SlackFileUploadEvent(
            channel="C123",
            filename="empty.txt",
        )

        with pytest.raises(ValueError, match="requires either content or file_path"):
            await integration.emit(event, context=mock_context)


class TestSlackEventsRegistration:
    def test_events_returns_all_event_types(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)
        events = integration.events()

        assert SlackMessageEvent in events
        assert SlackAppMentionEvent in events
        assert SlackReactionEvent in events
        assert SlackFileSharedEvent in events

    def test_register_adds_events_to_fastloop(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        app = FastLoop(name="test-app")
        integration = SlackIntegration(signing_secret="secret", setup=setup)

        @app.loop("testloop", integrations=[integration])
        async def test_loop(ctx):
            pass

        assert "slack_message" in app._event_types
        assert "slack_app_mention" in app._event_types
        assert "slack_reaction" in app._event_types
        assert "slack_file_shared" in app._event_types

    def test_register_adds_webhook_route(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        app = FastLoop(name="test-app")
        integration = SlackIntegration(signing_secret="secret", setup=setup)

        @app.loop("mybot", integrations=[integration])
        async def test_loop(ctx):
            pass

        route_paths = [route.path for route in app.routes]
        assert "/mybot/slack/events" in route_paths


class TestSlackEventTypes:
    def test_slack_message_event_has_correct_type(self):
        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="hello",
            ts="123.456",
            team="T789",
            event_ts="123.456",
        )
        assert event.type == "slack_message"

    def test_slack_app_mention_event_has_correct_type(self):
        event = SlackAppMentionEvent(
            channel="C123",
            user="U456",
            text="hello",
            ts="123.456",
            team="T789",
            event_ts="123.456",
        )
        assert event.type == "slack_app_mention"

    def test_slack_reaction_event_has_correct_type(self):
        event = SlackReactionEvent(
            channel="C123",
            user="U456",
            reaction="thumbsup",
            item_user="U789",
            event_ts="123.456",
        )
        assert event.type == "slack_reaction"

    def test_slack_file_shared_event_has_correct_type(self):
        event = SlackFileSharedEvent(
            file_id="F123",
            user="U456",
            channel="C789",
            event_ts="123.456",
        )
        assert event.type == "slack_file_shared"


class TestSlackIntegrationWithLoop:
    def test_loop_with_slack_integration_registers_correctly(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        app = FastLoop(name="test-app")

        @app.loop(
            "slackbot",
            integrations=[SlackIntegration(signing_secret="secret", setup=setup)],
        )
        async def slack_bot(ctx):
            pass

        assert "slackbot" in app._loop_metadata
        integrations = app._loop_metadata["slackbot"]["integrations"]
        assert len(integrations) == 1
        assert integrations[0].type() == IntegrationType.SLACK

    def test_multiple_loops_can_have_same_integration_type(self):
        async def setup1(_ctx, _event):
            return "xoxb-token-1"

        async def setup2(_ctx, _event):
            return "xoxb-token-2"

        app = FastLoop(name="test-app")

        @app.loop(
            "bot1",
            integrations=[SlackIntegration(signing_secret="secret1", setup=setup1)],
        )
        async def bot1(ctx):
            pass

        @app.loop(
            "bot2",
            integrations=[SlackIntegration(signing_secret="secret2", setup=setup2)],
        )
        async def bot2(ctx):
            pass

        assert "/bot1/slack/events" in [route.path for route in app.routes]
        assert "/bot2/slack/events" in [route.path for route in app.routes]


class TestUrlExtraction:
    def test_extract_plain_urls(self):
        text = "Check out https://example.com and http://test.org"
        urls = _extract_urls(text)
        assert "https://example.com" in urls
        assert "http://test.org" in urls

    def test_extract_slack_formatted_urls(self):
        text = "See <https://example.com|Example> for details"
        urls = _extract_urls(text)
        assert "https://example.com" in urls
        assert len(urls) == 1

    def test_extract_mixed_urls(self):
        text = "Visit <https://slack.com|Slack> or https://google.com"
        urls = _extract_urls(text)
        assert "https://slack.com" in urls
        assert "https://google.com" in urls

    def test_extract_deduplicates_urls(self):
        text = "https://example.com and <https://example.com|same link>"
        urls = _extract_urls(text)
        assert urls.count("https://example.com") == 1

    def test_extract_empty_text(self):
        assert _extract_urls("") == []
        assert _extract_urls(None) == []

    def test_extract_no_urls(self):
        text = "Just some plain text without links"
        assert _extract_urls(text) == []


class TestFilesParsing:
    def test_parse_empty_files(self):
        assert _parse_slack_files(None) == []
        assert _parse_slack_files([]) == []

    def test_parse_file_with_all_fields(self):
        files_raw = [
            {
                "id": "F12345",
                "name": "document.pdf",
                "mimetype": "application/pdf",
                "size": 1024,
                "url_private": "https://files.slack.com/private",
                "url_private_download": "https://files.slack.com/download",
                "permalink": "https://workspace.slack.com/files/F12345",
            }
        ]
        files = _parse_slack_files(files_raw)
        assert len(files) == 1
        assert files[0].id == "F12345"
        assert files[0].name == "document.pdf"
        assert files[0].mimetype == "application/pdf"
        assert files[0].size == 1024
        assert files[0].url_private == "https://files.slack.com/private"
        assert files[0].url_private_download == "https://files.slack.com/download"
        assert files[0].permalink == "https://workspace.slack.com/files/F12345"

    def test_parse_file_with_minimal_fields(self):
        files_raw = [{"id": "F99999"}]
        files = _parse_slack_files(files_raw)
        assert len(files) == 1
        assert files[0].id == "F99999"
        assert files[0].name is None
        assert files[0].mimetype is None

    def test_parse_multiple_files(self):
        files_raw = [
            {"id": "F1", "name": "file1.txt"},
            {"id": "F2", "name": "file2.jpg"},
            {"id": "F3", "name": "file3.mp4"},
        ]
        files = _parse_slack_files(files_raw)
        assert len(files) == 3
        assert [f.id for f in files] == ["F1", "F2", "F3"]


class TestSlackMessageEventRichFields:
    def test_root_ts_returns_thread_ts_when_present(self):
        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="reply in thread",
            ts="222.222",
            thread_ts="111.111",
            team="T789",
            event_ts="222.222",
        )
        assert event.root_ts == "111.111"

    def test_root_ts_returns_ts_when_no_thread_ts(self):
        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="new message",
            ts="333.333",
            team="T789",
            event_ts="333.333",
        )
        assert event.root_ts == "333.333"

    def test_message_with_files(self):
        files = [
            SlackFile(id="F1", name="image.png", mimetype="image/png"),
            SlackFile(id="F2", name="doc.pdf", mimetype="application/pdf"),
        ]
        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="here are files",
            ts="123.456",
            team="T789",
            event_ts="123.456",
            files=files,
        )
        assert len(event.files) == 2
        assert event.files[0].name == "image.png"

    def test_message_with_links(self):
        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="check this link",
            ts="123.456",
            team="T789",
            event_ts="123.456",
            links=["https://example.com", "https://test.org"],
        )
        assert len(event.links) == 2

    def test_message_with_blocks_and_attachments(self):
        blocks = [{"type": "section", "text": {"type": "plain_text", "text": "Block"}}]
        attachments = [{"color": "good", "text": "Attachment"}]
        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="rich message",
            ts="123.456",
            team="T789",
            event_ts="123.456",
            blocks=blocks,
            attachments=attachments,
        )
        assert len(event.blocks) == 1
        assert len(event.attachments) == 1

    def test_message_with_subtype_and_bot_id(self):
        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="bot message",
            ts="123.456",
            team="T789",
            event_ts="123.456",
            subtype="bot_message",
            bot_id="B12345",
        )
        assert event.subtype == "bot_message"
        assert event.bot_id == "B12345"

    def test_message_with_raw_event(self):
        raw = {"type": "message", "text": "raw", "extra_field": "value"}
        event = SlackMessageEvent(
            channel="C123",
            user="U456",
            text="with raw",
            ts="123.456",
            team="T789",
            event_ts="123.456",
            raw_event=raw,
        )
        assert event.raw_event == raw
        assert event.raw_event["extra_field"] == "value"


class TestSlackAppMentionEventRichFields:
    def test_root_ts_property(self):
        event = SlackAppMentionEvent(
            channel="C123",
            user="U456",
            text="@bot hello",
            ts="222.222",
            thread_ts="111.111",
            team="T789",
            event_ts="222.222",
        )
        assert event.root_ts == "111.111"

    def test_app_mention_with_files_and_links(self):
        files = [SlackFile(id="F1", name="attachment.txt")]
        event = SlackAppMentionEvent(
            channel="C123",
            user="U456",
            text="@bot check https://example.com",
            ts="123.456",
            team="T789",
            event_ts="123.456",
            files=files,
            links=["https://example.com"],
        )
        assert len(event.files) == 1
        assert len(event.links) == 1


class TestSlackLinkSharedEvent:
    def test_link_shared_event_has_correct_type(self):
        event = SlackLinkSharedEvent(
            channel="C123",
            user="U456",
            message_ts="123.456",
            links=[{"url": "https://example.com", "domain": "example.com"}],
            event_ts="123.456",
        )
        assert event.type == "slack_link_shared"

    def test_root_ts_returns_thread_ts_when_present(self):
        event = SlackLinkSharedEvent(
            channel="C123",
            user="U456",
            message_ts="222.222",
            thread_ts="111.111",
            links=[],
            event_ts="222.222",
        )
        assert event.root_ts == "111.111"

    def test_root_ts_returns_message_ts_when_no_thread_ts(self):
        event = SlackLinkSharedEvent(
            channel="C123",
            user="U456",
            message_ts="333.333",
            links=[],
            event_ts="333.333",
        )
        assert event.root_ts == "333.333"

    def test_urls_property_extracts_urls_from_links(self):
        event = SlackLinkSharedEvent(
            channel="C123",
            user="U456",
            message_ts="123.456",
            links=[
                {"url": "https://example.com", "domain": "example.com"},
                {"url": "https://test.org", "domain": "test.org"},
                {"domain": "no-url.com"},
            ],
            event_ts="123.456",
        )
        assert event.urls == ["https://example.com", "https://test.org"]


class TestSlackFileUploadEvent:
    def test_file_upload_event_has_correct_type(self):
        event = SlackFileUploadEvent(
            channel="C123",
            filename="test.txt",
            content=b"Hello",
        )
        assert event.type == "slack_file_upload"

    def test_file_upload_with_all_fields(self):
        event = SlackFileUploadEvent(
            channel="C123",
            thread_ts="123.456",
            filename="report.pdf",
            content=b"PDF content",
            title="Monthly Report",
            initial_comment="Here's the report",
        )
        assert event.channel == "C123"
        assert event.thread_ts == "123.456"
        assert event.filename == "report.pdf"
        assert event.content == b"PDF content"
        assert event.title == "Monthly Report"
        assert event.initial_comment == "Here's the report"


class TestIgnoredMessageSubtypes:
    def test_bot_message_is_ignored(self):
        assert "bot_message" in IGNORED_MESSAGE_SUBTYPES

    def test_message_changed_is_ignored(self):
        assert "message_changed" in IGNORED_MESSAGE_SUBTYPES

    def test_message_deleted_is_ignored(self):
        assert "message_deleted" in IGNORED_MESSAGE_SUBTYPES

    def test_channel_join_is_ignored(self):
        assert "channel_join" in IGNORED_MESSAGE_SUBTYPES


class TestSlackWebhookHandler:
    @pytest.fixture
    def integration_and_app(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        app = FastLoop(name="test-app")
        integration = SlackIntegration(signing_secret="secret", setup=setup)

        @app.loop("testbot", integrations=[integration])
        async def test_loop(ctx):
            pass

        return integration, app

    @pytest.mark.asyncio
    async def test_url_verification_returns_challenge(self, integration_and_app):
        integration, _ = integration_and_app

        mock_request = AsyncMock()
        mock_request.body = AsyncMock(
            return_value=b'{"type":"url_verification","challenge":"abc123"}'
        )
        mock_request.json = AsyncMock(
            return_value={"type": "url_verification", "challenge": "abc123"}
        )
        mock_request.headers = {}

        with patch.object(integration.verifier, "is_valid_request", return_value=True):
            result = await integration._handle_slack_event(mock_request)

        assert result == {"challenge": "abc123"}

    @pytest.mark.asyncio
    async def test_ignored_subtype_returns_ok_without_processing(
        self, integration_and_app
    ):
        integration, _ = integration_and_app

        for subtype in ["bot_message", "message_changed", "message_deleted"]:
            mock_request = AsyncMock()
            payload = {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "subtype": subtype,
                    "channel": "C123",
                    "user": "U456",
                    "text": "ignored",
                    "ts": "123.456",
                    "event_ts": "123.456",
                },
            }
            mock_request.body = AsyncMock(return_value=b"{}")
            mock_request.json = AsyncMock(return_value=payload)
            mock_request.headers = {}

            with patch.object(
                integration.verifier, "is_valid_request", return_value=True
            ):
                result = await integration._handle_slack_event(mock_request)

            assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_bot_id_message_is_ignored(self, integration_and_app):
        integration, _ = integration_and_app

        mock_request = AsyncMock()
        payload = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U456",
                "text": "bot message",
                "ts": "123.456",
                "event_ts": "123.456",
                "bot_id": "B12345",
            },
        }
        mock_request.body = AsyncMock(return_value=b"{}")
        mock_request.json = AsyncMock(return_value=payload)
        mock_request.headers = {}

        with patch.object(integration.verifier, "is_valid_request", return_value=True):
            result = await integration._handle_slack_event(mock_request)

        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_unsupported_event_type_returns_ok(self, integration_and_app):
        integration, _ = integration_and_app

        mock_request = AsyncMock()
        payload = {
            "type": "event_callback",
            "event": {
                "type": "channel_created",
                "channel": {"id": "C123"},
            },
        }
        mock_request.body = AsyncMock(return_value=b"{}")
        mock_request.json = AsyncMock(return_value=payload)
        mock_request.headers = {}

        with patch.object(integration.verifier, "is_valid_request", return_value=True):
            result = await integration._handle_slack_event(mock_request)

        assert result == {"ok": True}


class TestSlackEventsRegistrationExpanded:
    def test_events_includes_new_event_types(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        integration = SlackIntegration(signing_secret="secret", setup=setup)
        events = integration.events()

        assert SlackLinkSharedEvent in events
        assert SlackFileUploadEvent in events

    def test_register_adds_new_event_types_to_fastloop(self):
        async def setup(_ctx, _event):
            return "xoxb-token"

        app = FastLoop(name="test-app")
        integration = SlackIntegration(signing_secret="secret", setup=setup)

        @app.loop("testloop", integrations=[integration])
        async def test_loop(ctx):
            pass

        assert "slack_link_shared" in app._event_types
        assert "slack_file_upload" in app._event_types
