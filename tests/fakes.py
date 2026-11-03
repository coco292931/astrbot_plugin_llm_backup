"""测试用 AstrBot 对象替身，只保留插件实际读取的字段。"""

from __future__ import annotations

from typing import Any


class FakeComponent:
    def __init__(self, kind: str, **fields: Any) -> None:
        self.type = kind
        for key, value in fields.items():
            setattr(self, key, value)


class FakeFileComponent:
    """刻意不提供可读的 file 属性，用于确认实现不会触发同步下载。"""

    def __init__(self, name: str, file_: str = "", url: str = "") -> None:
        self.type = "File"
        self.name = name
        self.file_ = file_
        self.url = url

    @property
    def file(self) -> str:
        raise AssertionError("不应访问 File.file")


class FakeEnumType:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeMessageObject:
    def __init__(
        self,
        message: list[Any] | None = None,
        *,
        message_id: str = "msg-1",
        msg_type: str = "FriendMessage",
        sender_id: str = "10001",
        sender_name: str = "alice",
        group_id: str = "",
        timestamp: int = 0,
    ) -> None:
        self.message = message or []
        self.message_id = message_id
        self.type = msg_type
        self.timestamp = timestamp
        self.sender = type(
            "Sender",
            (),
            {"user_id": sender_id, "nickname": sender_name},
        )()
        self.group = (
            type("Group", (), {"group_id": group_id})() if group_id else None
        )


class FakeEvent:
    def __init__(
        self,
        *,
        umo: str = "aiocqhttp:FriendMessage:10001",
        message: list[Any] | None = None,
        message_id: str = "msg-1",
        msg_type: str = "FriendMessage",
        sender_id: str = "10001",
        sender_name: str = "alice",
        group_id: str = "",
        platform: str = "aiocqhttp",
        extras: dict[str, Any] | None = None,
    ) -> None:
        self.unified_msg_origin = umo
        self.platform_name = platform
        self.created_at = 0.0
        self.message_obj = FakeMessageObject(
            message,
            message_id=message_id,
            msg_type=msg_type,
            sender_id=sender_id,
            sender_name=sender_name,
            group_id=group_id,
        )
        self.message_str = "".join(
            item.text for item in (message or []) if getattr(item, "type", "") == "Plain"
        )
        self._extras = dict(extras or {})

    def get_platform_name(self) -> str:
        return self.platform_name

    def is_private_chat(self) -> bool:
        return "friend" in str(self.message_obj.type).lower()

    def get_sender_id(self) -> str:
        return self.message_obj.sender.user_id

    def get_sender_name(self) -> str:
        return self.message_obj.sender.nickname

    def get_group_id(self) -> str:
        return self.message_obj.group.group_id if self.message_obj.group else ""

    def get_message_str(self) -> str:
        return self.message_str

    def get_messages(self) -> list[Any]:
        return list(self.message_obj.message)

    def get_extra(self, key: str | None = None, default: Any = None) -> Any:
        if key is None:
            return dict(self._extras)
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    def plain_result(self, text: str) -> str:
        return text


class FakeConversation:
    def __init__(
        self,
        cid: str = "conv-1",
        title: str = "默认对话",
        persona_id: str | None = None,
    ) -> None:
        self.cid = cid
        self.title = title
        self.persona_id = persona_id


class FakeToolSet:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def names(self) -> list[str]:
        return list(self._names)


class FakeRequest:
    def __init__(
        self,
        *,
        prompt: str = "你好",
        contexts: list[Any] | None = None,
        system_prompt: str = "you are a bot",
        model: str = "test-model",
        conversation: Any = None,
        func_tool: Any = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
    ) -> None:
        self.prompt = prompt
        self.contexts = contexts or []
        self.system_prompt = system_prompt
        self.model = model
        self.conversation = conversation
        self.func_tool = func_tool
        self.image_urls = image_urls or []
        self.audio_urls = audio_urls or []


class FakePart:
    def __init__(self, kind: str, **fields: Any) -> None:
        self.type = kind
        for key, value in fields.items():
            setattr(self, key, value)


class FakeMessage:
    def __init__(
        self,
        role: str,
        content: Any = None,
        tool_calls: list[Any] | None = None,
    ) -> None:
        self.role = role
        self.content = content
        self.tool_calls = tool_calls or []


class FakeRunContext:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages


class FakeUsage:
    def __init__(self, input_tokens: int = 10, output_tokens: int = 20) -> None:
        self.input = input_tokens
        self.output = output_tokens


class FakeResponse:
    def __init__(
        self,
        *,
        role: str = "assistant",
        reasoning: str | None = None,
        text: str = "",
        usage: Any = None,
    ) -> None:
        self.role = role
        self.reasoning_content = reasoning
        self.completion_text = text
        self.result_chain = None
        self.usage = usage


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeToolResult:
    def __init__(self, texts: list[str], is_error: bool = False) -> None:
        self.content = [FakePart("text", text=text) for text in texts]
        self.isError = is_error
