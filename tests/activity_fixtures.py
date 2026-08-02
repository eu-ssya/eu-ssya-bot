import datetime
import inspect
import tempfile
import unittest.mock
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import discord

from activity_store import (
    ActivityReport,
    ActivityStore,
    CoverageWarning,
    ReportRow,
)


class FakeBot:
    def __init__(self, fail_extension=None):
        self.fail_extension, self.calls, self.guilds = fail_extension, [], []

    async def load_extension(self, name):
        self.calls.append(name)
        if name == self.fail_extension:
            raise RuntimeError(name)

    def start_rss(self):
        self.calls.append("rss_loop.start")

    async def wait_until_ready(self):
        return None


@dataclass
class FakeResponse:
    sent: list = field(default_factory=list)
    edits: list = field(default_factory=list)
    deferred: bool = False
    defer_kwargs: dict = field(default_factory=dict)
    done: bool = False

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.sent.append((content, kwargs))

    async def send(self, content=None, **kwargs):
        self.done = True
        self.sent.append((content, kwargs))

    async def edit_message(self, **kwargs):
        self.done = True
        self.edits.append(kwargs)

    async def defer(self, **kwargs):
        self.deferred, self.defer_kwargs, self.done = True, kwargs, True

    def is_done(self):
        return self.done


def fake_interaction(user_id, administrator, guild=None):
    permissions = SimpleNamespace(administrator=administrator)
    guild = guild if guild is not None else FakeGuild(1)
    followup = FakeResponse()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=user_id, guild_permissions=permissions),
        response=FakeResponse(),
        files=[],
        guild=guild,
        followup=followup,
        original_edits=[],
    )

    async def edit_original_response(**kwargs):
        interaction.original_edits.append(kwargs)

    interaction.edit_original_response = edit_original_response
    return interaction


def fake_deferred_interaction(user_id, administrator, guild=None):
    return fake_interaction(user_id, administrator, guild)


def fake_button():
    return SimpleNamespace(disabled=False)


async def press(view, item, interaction):
    if await view.interaction_check(interaction):
        await item.callback(interaction)


class FakeOriginalResponse:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


def fake_message():
    return FakeOriginalResponse()


def configured_fixture():
    return build_fake_configured_cog(
        target_role_id=10,
        reading_category_id=20,
        study_category_id=30,
        sod_eod_channel_id=40,
    )


@dataclass
class FakeMember:
    id: int
    guild: object
    role_ids: set[int]
    category_id: int | None = None
    bot: bool = False
    display_name: str = "Fake Member"

    @property
    def roles(self):
        return [SimpleNamespace(id=role_id) for role_id in self.role_ids]

    @property
    def voice(self):
        return SimpleNamespace(channel=SimpleNamespace(category_id=self.category_id))

    def in_category(self, category_id):
        return FakeMember(
            self.id,
            self.guild,
            set(self.role_ids),
            category_id,
            self.bot,
            self.display_name,
        )

    def with_roles(self, role_ids):
        return FakeMember(
            self.id,
            self.guild,
            set(role_ids),
            self.category_id,
            self.bot,
            self.display_name,
        )


class FakeGuild:
    def __init__(self, guild_id):
        self.id, self.members, self.roles, self.channels = guild_id, [], [], []
        self.me = SimpleNamespace(id=999)

    def get_member(self, user_id):
        return next((member for member in self.members if member.id == user_id), None)

    async def fetch_member(self, user_id):
        member = self.get_member(user_id)
        if member is None:
            raise discord.NotFound(SimpleNamespace(status=404), "member")
        return member

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_channel(self, channel_id):
        return next((channel for channel in self.channels if channel.id == channel_id), None)


def fake_role(role_id, guild):
    value = Mock(spec=discord.Role)
    value.id, value.guild = role_id, guild
    return value


def fake_category(channel_id, guild, can_read=True):
    value = Mock(spec=discord.CategoryChannel)
    value.id, value.guild = channel_id, guild
    value.permissions_for.return_value = SimpleNamespace(
        view_channel=can_read,
        read_message_history=can_read,
    )
    return value


def fake_text_channel(channel_id, guild, can_read=True):
    value = Mock(spec=discord.TextChannel)
    value.id, value.guild = channel_id, guild
    value.permissions_for.return_value = SimpleNamespace(
        view_channel=can_read,
        read_message_history=can_read,
    )
    return value


def build_fake_configured_cog(
    target_role_id,
    reading_category_id,
    study_category_id,
    sod_eod_channel_id,
):
    from activity_cog import ActivityCog

    tmp = tempfile.TemporaryDirectory()
    store = ActivityStore(str(Path(tmp.name) / "activity.db"))
    store.initialize()
    guild = FakeGuild(1)
    guild.roles = [fake_role(target_role_id, guild)]
    guild.channels = [
        fake_category(reading_category_id, guild),
        fake_category(study_category_id, guild),
        fake_category(21, guild),
        fake_text_channel(sod_eod_channel_id, guild),
    ]
    member = FakeMember(1, guild, {target_role_id}, reading_category_id)
    guild.members = [member]
    bot = FakeBot()
    bot.guilds = [guild]
    cog = ActivityCog(bot, store)
    cog._test_tmp = tmp
    store.apply_config_change(
        1,
        target_role_id=target_role_id,
        reading_category_id=reading_category_id,
        study_category_id=study_category_id,
        sod_eod_channel_id=sod_eod_channel_id,
        effective_at_epoch=1,
    )
    cog.collection_gates[1].set()
    store.open_collection_run(1, 1)
    return cog, guild, member


def sample_report():
    return make_report([])


def report_with_members(count):
    return make_report(
        [
            ReportRow(
                user_id=index,
                display_name=str(index),
                last_activity_epoch=None,
                reading_seconds=0,
                study_seconds=0,
                reading_session_count=0,
                study_session_count=0,
                sod_days=0,
                eod_days=0,
                combined_days=0,
            )
            for index in range(count)
        ]
    )


def report_with_warning(code, text):
    return make_report([], warnings=[CoverageWarning(code=code, text=text)])


def make_report(rows, warnings=None):
    return ActivityReport(
        rows=rows,
        warnings=warnings or [],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        start_epoch=0,
        end_epoch=172800,
        generated_epoch=172800,
        period_label="조회 기간: 2026-08-01 ~ 2026-08-02",
        txt_filename="activity-report-20260801-20260802-kst.txt",
        page_count=max(1, (len(rows) + 14) // 15),
    )


def fixture_channel_with_missing_author():
    author = SimpleNamespace(id=99, bot=False)
    message = SimpleNamespace(
        id=12,
        author=author,
        created_at=datetime.datetime(2026, 8, 2, tzinfo=datetime.UTC),
        content="SoD",
    )

    class Channel:
        id = 40

        async def history(self, **kwargs):
            yield message

    return Channel()


def make_message(message_id, member, content):
    return SimpleNamespace(
        id=message_id,
        author=member,
        content=content,
        created_at=datetime.datetime(2026, 8, 2, tzinfo=datetime.UTC),
        type=discord.MessageType.default,
    )


def make_history_channel(guild, channel_id, messages, before_first_yield=None):
    channel = Mock(spec=discord.TextChannel)
    channel.id, channel.guild = channel_id, guild

    async def history(**kwargs):
        if before_first_yield is not None:
            result = before_first_yield()
            if inspect.isawaitable(result):
                await result
        for message in messages:
            yield message

    channel.history.side_effect = history
    return channel


def make_controlled_history_channel(guild, channel_id, messages, entered, release):
    channel = Mock(spec=discord.TextChannel)
    channel.id, channel.guild = channel_id, guild

    async def history(**kwargs):
        entered.set()
        await release.wait()
        for message in messages:
            yield message

    channel.history.side_effect = history
    return channel


async def prepare_sync_marker(cog, channel_id, message_id):
    await cog._store_call(
        cog.store.record_backfill_message_and_advance_cursor,
        guild_id=1,
        channel_id=channel_id,
        message_id=message_id,
        user_id=1,
        message_created_epoch=99,
        event_types=set(),
        newest_processed_message_created_epoch=99,
        updated_epoch=100,
        expected_current_channel_id=channel_id,
    )


async def session_rows(cog, user_id):
    return await cog._store_call(cog.store.list_sessions, 1, user_id)


async def voice_rows(cog):
    return await cog._store_call(cog.store.list_sessions_for_guild, 1)


async def open_fixture_session(cog, user_id, kind, started_epoch):
    await cog._store_call(cog.store.reconcile_session, 1, user_id, kind, started_epoch)


async def coverage_gaps(cog, start_epoch, end_epoch):
    coverage = await cog._store_call(
        cog.store.voice_coverage_for_range,
        1,
        start_epoch,
        end_epoch,
    )
    return coverage.gaps


async def event_count(cog):
    return await cog._store_call(cog.store.event_count, 1)


async def sync_state(cog, channel_id):
    return await cog._store_call(cog.store.get_sync_state, 1, channel_id)


async def open_session_count(cog, user_id):
    return await cog._store_call(cog.store.open_session_count, 1, user_id)


async def set_reading_for_test(cog, guild, category_id, now_epoch):
    await cog._change_voice_setting(
        guild,
        now_epoch,
        reading_category_id=category_id,
    )


async def set_sod_channel_for_test(cog, guild, channel_id, now_epoch):
    await cog._change_sod_setting(guild, now_epoch, channel_id)


async def on_disconnect_for_test(cog, now_epoch):
    with unittest.mock.patch("activity_cog.utc_now_epoch", return_value=now_epoch):
        await cog.on_disconnect()


async def on_resumed_for_test(cog, now_epoch):
    with unittest.mock.patch("activity_cog.utc_now_epoch", return_value=now_epoch):
        await cog.on_resumed()


async def recover_after_ready_for_test(cog, now_epoch):
    with unittest.mock.patch("activity_cog.utc_now_epoch", return_value=now_epoch):
        await cog.recover_after_ready()


async def record_live_message_for_test(cog, message_id, content):
    from activity_cog import detect_sod_eod

    config = await cog._store_call(cog.store.get_config, 1)
    await cog._store_call(
        cog.store.record_live_message,
        guild_id=1,
        channel_id=config.sod_eod_channel_id,
        message_id=message_id,
        user_id=1,
        message_created_epoch=100,
        event_types=detect_sod_eod(content),
        updated_epoch=101,
        expected_current_channel_id=config.sod_eod_channel_id,
    )


async def checkpoint_once_for_test(cog):
    await cog._checkpoint_open_rows_once()


async def run_checkpoint(cog, guild_id):
    return await cog._store_call(cog.store.get_open_run_checkpoint, guild_id)
