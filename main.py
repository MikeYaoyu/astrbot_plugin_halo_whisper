from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .halo_client import HaloClient, HaloClientError


@dataclass
class MomentDraft:
    content_parts: list[str] = field(default_factory=list)
    image_paths: list[Path] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)
    publishing: bool = False

    @property
    def content(self) -> str:
        return "\n".join(part for part in self.content_parts if part).strip()


@register(
    "astrbot_plugin_halo_whisper",
    "MikeYaoyu",
    "通过 AstrBot 发布文字和图片到 Halo 瞬间",
    "1.1.0",
)
class HaloWhisperPlugin(Star):
    DRAFT_COMMANDS = {
        "halo",
        "halo开始",
        "halo发布",
        "halo预览",
        "halo取消",
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._drafts: dict[str, MomentDraft] = {}
        self._draft_dir = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_halo_whisper"
            / "drafts"
        )

    async def initialize(self):
        self._draft_dir.mkdir(parents=True, exist_ok=True)
        await self._remove_stale_draft_files()

    @filter.command("halo")
    async def publish_moment(self, event: AstrMessageEvent):
        """立即发布当前消息中的文字和图片到 Halo 瞬间"""
        permission_error = self._permission_error(event)
        if permission_error:
            yield event.plain_result(permission_error)
            return

        content = self._extract_content(event.message_str)
        images = self._message_images(event)
        if not content and not images:
            yield event.plain_result(
                "请在 /halo 后输入内容，或使用 /halo开始 进入草稿收集模式。"
            )
            return
        if len(images) > self._max_images():
            yield event.plain_result(
                f"图片数量超过限制：当前 {len(images)} 张，"
                f"最多 {self._max_images()} 张。"
            )
            return

        client_or_error = self._create_client()
        if isinstance(client_or_error, str):
            yield event.plain_result(client_or_error)
            return

        try:
            image_paths = [
                Path(await image.convert_to_file_path()) for image in images
            ]
            moment = await self._publish(client_or_error, content, image_paths)
        except (HaloClientError, OSError, ValueError) as exc:
            logger.warning(f"发布 Halo 瞬间失败: {exc}")
            yield event.plain_result(f"发布失败：{exc}")
            return
        except Exception:
            logger.exception("发布 Halo 瞬间时出现未预期错误")
            yield event.plain_result("发布失败：发生未预期错误，请查看 AstrBot 日志。")
            return

        yield event.plain_result(self._success_message(moment))

    @filter.command("halo开始")
    async def start_draft(self, event: AstrMessageEvent):
        """开始收集一条 Halo 瞬间草稿"""
        permission_error = self._permission_error(event)
        if permission_error:
            yield event.plain_result(permission_error)
            return

        key = self._draft_key(event)
        if key in self._drafts:
            yield event.plain_result(
                "你已经有一条 Halo 草稿，请发送 /halo发布、"
                "/halo预览 或 /halo取消。"
            )
            return

        content = self._extract_content(event.message_str)
        images = self._message_images(event)
        if len(images) > self._max_images():
            yield event.plain_result(
                f"图片数量超过限制，最多允许 {self._max_images()} 张。"
            )
            return

        try:
            image_paths = await self._materialize_images(images)
        except (OSError, ValueError) as exc:
            yield event.plain_result(f"草稿创建失败：{exc}")
            return

        draft = MomentDraft(
            content_parts=[content] if content else [],
            image_paths=image_paths,
        )
        self._drafts[key] = draft
        timeout = self._draft_timeout_seconds()

        yield event.plain_result(
            "Halo 草稿已开始。接下来可分别发送文字或图片，"
            "完成后发送 /halo发布。\n"
            + self._draft_summary(draft)
        )

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def draft_waiter(
            controller: SessionController,
            incoming_event: AstrMessageEvent,
        ):
            await self._handle_draft_message(
                controller,
                incoming_event,
                key,
                draft,
                timeout,
            )

        try:
            await draft_waiter(event)
        except TimeoutError:
            if self._drafts.get(key) is draft:
                self._drafts.pop(key, None)
                await self._delete_files(draft.image_paths)
                yield event.plain_result("Halo 草稿长时间未操作，已自动清理。")
        except Exception:
            logger.exception("Halo 草稿会话出现未预期错误")
            if self._drafts.get(key) is draft:
                self._drafts.pop(key, None)
                await self._delete_files(draft.image_paths)
            yield event.plain_result("Halo 草稿会话异常结束，请重新发送 /halo开始。")
        finally:
            event.stop_event()

    async def _handle_draft_message(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        key: str,
        draft: MomentDraft,
        timeout: int,
    ):
        command = self._command_name(event.message_str)

        if command == "halo取消":
            self._drafts.pop(key, None)
            await self._delete_files(draft.image_paths)
            await event.send(event.plain_result("Halo 草稿已取消。"))
            controller.stop()
            return

        if command == "halo预览":
            await event.send(event.plain_result(self._preview_message(draft)))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        if command == "halo发布":
            await self._publish_waiting_draft(
                controller,
                event,
                key,
                draft,
                timeout,
            )
            return

        if command in self.DRAFT_COMMANDS:
            await event.send(
                event.plain_result(
                    "当前正在收集草稿。请发送文字或图片，完成后发送 "
                    "/halo发布；也可以使用 /halo预览 或 /halo取消。"
                )
            )
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        content = (event.message_str or "").strip()
        images = self._message_images(event)
        if not content and not images:
            await event.send(
                event.plain_result("这条消息没有可加入草稿的文字或图片。")
            )
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        remaining = self._max_images() - len(draft.image_paths)
        if len(images) > remaining:
            await event.send(
                event.plain_result(
                    f"这些图片没有加入草稿：还可添加 {max(0, remaining)} 张，"
                    f"本次发送了 {len(images)} 张。"
                )
            )
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        try:
            new_paths = await self._materialize_images(images)
        except (OSError, ValueError) as exc:
            await event.send(event.plain_result(f"图片没有加入草稿：{exc}"))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        if content:
            draft.content_parts.append(content)
        draft.image_paths.extend(new_paths)
        draft.updated_at = time.monotonic()
        await event.send(
            event.plain_result("已加入 Halo 草稿。\n" + self._draft_summary(draft))
        )
        controller.keep(timeout=timeout, reset_timeout=True)

    async def _publish_waiting_draft(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        key: str,
        draft: MomentDraft,
        timeout: int,
    ):
        if draft.publishing:
            await event.send(event.plain_result("草稿正在发布，请勿重复操作。"))
            controller.keep(timeout=timeout, reset_timeout=True)
            return
        if not draft.content and not draft.image_paths:
            await event.send(
                event.plain_result("草稿还是空的，请先发送文字或图片。")
            )
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        client_or_error = self._create_client()
        if isinstance(client_or_error, str):
            await event.send(event.plain_result(client_or_error))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        draft.publishing = True
        try:
            moment = await self._publish(
                client_or_error,
                draft.content,
                list(draft.image_paths),
            )
        except (HaloClientError, OSError, ValueError) as exc:
            draft.publishing = False
            logger.warning(f"发布 Halo 草稿失败: {exc}")
            await event.send(
                event.plain_result(f"发布失败：{exc}\n草稿已保留，可以稍后重试。")
            )
            controller.keep(timeout=timeout, reset_timeout=True)
            return
        except Exception:
            draft.publishing = False
            logger.exception("发布 Halo 草稿时出现未预期错误")
            await event.send(
                event.plain_result(
                    "发布失败：发生未预期错误，草稿已保留，请查看 AstrBot 日志。"
                )
            )
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        self._drafts.pop(key, None)
        await self._delete_files(draft.image_paths)
        await event.send(event.plain_result(self._success_message(moment)))
        controller.stop()

    @filter.command("halo预览")
    async def preview_without_draft(self, event: AstrMessageEvent):
        """提示如何查看 Halo 草稿"""
        yield event.plain_result("当前没有 Halo 草稿。发送 /halo开始 可新建草稿。")

    @filter.command("halo发布")
    async def publish_without_draft(self, event: AstrMessageEvent):
        """提示如何发布 Halo 草稿"""
        yield event.plain_result("当前没有 Halo 草稿。发送 /halo开始 可新建草稿。")

    @filter.command("halo取消")
    async def cancel_without_draft(self, event: AstrMessageEvent):
        """提示当前没有 Halo 草稿"""
        yield event.plain_result("当前没有 Halo 草稿。")

    async def _publish(
        self,
        client: HaloClient,
        content: str,
        image_paths: list[Path],
    ) -> dict[str, Any]:
        media_urls = await self._upload_paths(client, image_paths)
        return await client.create_moment(
            content=content,
            image_urls=media_urls,
            tags=self._configured_tags(),
            visibility=str(self.config.get("visibility", "PUBLIC")),
        )

    async def _upload_paths(
        self,
        client: HaloClient,
        image_paths: list[Path],
    ) -> list[str]:
        uploaded_urls: list[str] = []
        max_size_mb = max(1, int(self.config.get("max_image_size_mb", 20)))
        max_bytes = max_size_mb * 1024 * 1024
        policy_name = str(self.config.get("storage_policy_name", "")).strip()
        group_name = str(self.config.get("attachment_group_name", "")).strip()

        for index, local_path in enumerate(image_paths, start=1):
            if not local_path.is_file():
                raise ValueError(f"第 {index} 张图片无法读取")
            if local_path.stat().st_size > max_bytes:
                raise ValueError(f"第 {index} 张图片超过 {max_size_mb} MB")
            uploaded_urls.append(
                await client.upload_image(
                    local_path,
                    policy_name=policy_name,
                    group_name=group_name,
                )
            )
        return uploaded_urls

    async def _materialize_images(
        self,
        images: list[Comp.Image],
    ) -> list[Path]:
        copied: list[Path] = []
        max_size_mb = max(1, int(self.config.get("max_image_size_mb", 20)))
        max_bytes = max_size_mb * 1024 * 1024
        self._draft_dir.mkdir(parents=True, exist_ok=True)

        try:
            for index, image in enumerate(images, start=1):
                source = Path(await image.convert_to_file_path())
                if not source.is_file():
                    raise ValueError(f"第 {index} 张图片无法读取")
                if source.stat().st_size > max_bytes:
                    raise ValueError(f"第 {index} 张图片超过 {max_size_mb} MB")

                suffix = source.suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                    suffix = ".img"
                target = self._draft_dir / f"{uuid.uuid4().hex}{suffix}"
                await asyncio.to_thread(shutil.copy2, source, target)
                copied.append(target)
        except Exception:
            await self._delete_files(copied)
            raise

        return copied

    async def _remove_stale_draft_files(self):
        if not self._draft_dir.exists():
            return
        cutoff = time.time() - self._draft_timeout_seconds()
        stale_files = [
            path
            for path in self._draft_dir.iterdir()
            if path.is_file() and path.stat().st_mtime < cutoff
        ]
        await self._delete_files(stale_files)

    @staticmethod
    async def _delete_files(paths: list[Path]):
        def delete():
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(f"无法清理 Halo 草稿图片: {path}")

        await asyncio.to_thread(delete)

    def _create_client(self) -> HaloClient | str:
        base_url = str(self.config.get("halo_url", "")).strip()
        token = str(self.config.get("api_token", "")).strip()
        if not base_url or not token:
            return (
                "插件尚未配置 Halo 地址或个人访问令牌，"
                "请先在 AstrBot WebUI 中完成配置。"
            )
        return HaloClient(
            base_url=base_url,
            token=token,
            timeout=max(1, int(self.config.get("request_timeout", 30))),
            verify_ssl=bool(self.config.get("verify_ssl", True)),
        )

    def _success_message(self, moment: dict[str, Any]) -> str:
        base_url = str(self.config.get("halo_url", "")).strip().rstrip("/")
        name = moment.get("metadata", {}).get("name", "")
        permalink = moment.get("status", {}).get("permalink")
        if not permalink and name:
            permalink = f"{base_url}/moments/{name}"
        return "Halo 瞬间发布成功。" + (f"\n{permalink}" if permalink else "")

    def _permission_error(self, event: AstrMessageEvent) -> str | None:
        if self.config.get("admin_only", True) and not event.is_admin():
            return "你没有发布 Halo 瞬间的权限。"
        allowed_users = {
            str(user_id).strip()
            for user_id in self.config.get("allowed_user_ids", [])
            if str(user_id).strip()
        }
        if allowed_users and str(event.get_sender_id()) not in allowed_users:
            return "你不在 Halo 瞬间发布白名单中。"
        return None

    def _configured_tags(self) -> list[str]:
        tags: Any = self.config.get("default_tags", [])
        if isinstance(tags, str):
            tags = tags.split(",")
        return list(
            dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip())
        )

    def _preview_message(self, draft: MomentDraft) -> str:
        content_preview = draft.content
        if len(content_preview) > 200:
            content_preview = content_preview[:200] + "…"
        return self._draft_summary(draft) + (
            f"\n\n文字预览：\n{content_preview}" if content_preview else ""
        )

    def _draft_summary(self, draft: MomentDraft) -> str:
        timeout_minutes = max(
            1,
            int(self.config.get("draft_timeout_minutes", 30)),
        )
        return (
            f"文字 {len(draft.content)} 字，图片 {len(draft.image_paths)}"
            f"/{self._max_images()} 张；{timeout_minutes} 分钟无操作后自动清理。"
        )

    def _draft_timeout_seconds(self) -> int:
        minutes = max(1, int(self.config.get("draft_timeout_minutes", 30)))
        return minutes * 60

    def _max_images(self) -> int:
        return max(0, int(self.config.get("max_images", 9)))

    @staticmethod
    def _message_images(event: AstrMessageEvent) -> list[Comp.Image]:
        return [
            component
            for component in event.get_messages()
            if isinstance(component, Comp.Image)
        ]

    @staticmethod
    def _draft_key(event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"

    @staticmethod
    def _command_name(message: str) -> str:
        stripped = (message or "").strip()
        if not stripped:
            return ""
        return stripped.split(maxsplit=1)[0].lstrip("/!#.")

    @staticmethod
    def _extract_content(message: str) -> str:
        """移除消息开头的指令词，保留其后的完整文本。"""
        parts = (message or "").strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) == 2 else ""

    async def terminate(self):
        drafts = list(self._drafts.values())
        self._drafts.clear()
        await self._delete_files(
            [path for draft in drafts for path in draft.image_paths]
        )
