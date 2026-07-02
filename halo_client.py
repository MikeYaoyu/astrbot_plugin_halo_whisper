from __future__ import annotations

import html
import mimetypes
from pathlib import Path
from typing import Any

import aiohttp


class HaloClientError(RuntimeError):
    """Halo API 返回了无法完成请求的响应。"""


class HaloClient:
    ATTACHMENT_ENDPOINT = "/apis/api.console.halo.run/v1alpha1/attachments/upload"
    POLICY_ENDPOINT = "/apis/storage.halo.run/v1alpha1/policies"
    MOMENT_ENDPOINT = "/apis/console.api.moment.halo.run/v1alpha1/moments"

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int = 30,
        verify_ssl: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token.removeprefix("Bearer ").strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.verify_ssl = verify_ssl
        self._auto_policy_name: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    async def upload_image(
        self,
        path: Path,
        policy_name: str = "",
        group_name: str = "",
    ) -> str:
        policy_name = policy_name.strip()
        if not policy_name:
            policy_name = await self._resolve_single_storage_policy()

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form = aiohttp.FormData()

        with path.open("rb") as image_file:
            form.add_field(
                "file",
                image_file,
                filename=path.name,
                content_type=content_type,
            )
            form.add_field("policyName", policy_name)
            if group_name:
                form.add_field("groupName", group_name)

            try:
                payload = await self._request(
                    "POST",
                    self.ATTACHMENT_ENDPOINT,
                    data=form,
                )
            except HaloClientError as exc:
                if "policyName" in str(exc):
                    raise HaloClientError(
                        "Halo 拒绝了附件存储策略。请在插件配置中填写有效的"
                        "“附件存储策略名称”（Policy 的 metadata.name）。"
                    ) from exc
                raise

        permalink = payload.get("status", {}).get("permalink")
        if not permalink:
            raise HaloClientError("Halo 上传成功，但响应中没有图片永久链接")
        return str(permalink)

    async def _resolve_single_storage_policy(self) -> str:
        if self._auto_policy_name:
            return self._auto_policy_name

        try:
            payload = await self._request(
                "GET",
                self.POLICY_ENDPOINT,
                params={"page": 0, "size": 100},
            )
        except HaloClientError as exc:
            raise HaloClientError(
                "未配置附件存储策略，且无法从 Halo 自动读取。"
                "请在插件配置中填写 Policy 的 metadata.name。"
            ) from exc

        policies = []
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("metadata", {}).get("name", "")).strip()
            if not name:
                continue
            display_name = str(item.get("spec", {}).get("displayName", "")).strip()
            policies.append((name, display_name))

        if len(policies) == 1:
            self._auto_policy_name = policies[0][0]
            return self._auto_policy_name
        if not policies:
            raise HaloClientError(
                "Halo 中没有可用的附件存储策略，请先在 Halo Console 创建一个。"
            )

        choices = "、".join(
            f"{display_name}（{name}）" if display_name else name
            for name, display_name in policies
        )
        raise HaloClientError(
            "Halo 中存在多个附件存储策略，请在插件配置的"
            f"“附件存储策略名称”中填写其中一个 metadata.name：{choices}"
        )

    async def create_moment(
        self,
        content: str,
        image_urls: list[str],
        tags: list[str],
        visibility: str,
    ) -> dict[str, Any]:
        rendered_content = self._render_text(content)
        payload = {
            "apiVersion": "moment.halo.run/v1alpha1",
            "kind": "Moment",
            "metadata": {"generateName": "moment-"},
            "spec": {
                "content": {
                    "raw": rendered_content,
                    "html": rendered_content,
                    "medium": [
                        {
                            "type": "PHOTO",
                            "url": image_url,
                            "originType": "attachment",
                        }
                        for image_url in image_urls
                    ],
                },
                "visible": (
                    visibility if visibility in {"PUBLIC", "PRIVATE"} else "PUBLIC"
                ),
                "owner": "",
                "tags": tags,
                "approved": True,
            },
        }
        return await self._request("POST", self.MOMENT_ENDPOINT, json=payload)

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        try:
            async with aiohttp.ClientSession(
                headers=self.headers,
                timeout=self.timeout,
            ) as session:
                async with session.request(
                    method,
                    url,
                    ssl=self.verify_ssl,
                    **kwargs,
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        detail = (await response.text()).strip()
                        if len(detail) > 300:
                            detail = detail[:300] + "…"
                        raise HaloClientError(
                            f"Halo API 返回 HTTP {response.status}"
                            + (f"：{detail}" if detail else "")
                        )
                    try:
                        payload = await response.json()
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise HaloClientError("Halo API 返回了无效的 JSON") from exc
        except aiohttp.ClientError as exc:
            raise HaloClientError(f"无法连接 Halo：{exc}") from exc

        if not isinstance(payload, dict):
            raise HaloClientError("Halo API 响应格式不正确")
        return payload

    @staticmethod
    def _render_text(content: str) -> str:
        if not content:
            return ""
        return "<p>" + html.escape(content).replace("\n", "<br>") + "</p>"
