"""json-repair 的本地精确类型存根。"""

from typing import Literal, overload

type JSONRepairValue = dict[str, object] | list[object] | str | float | int | bool | None

@overload
def repair_json(
    json_str: str = "",
    *,
    return_objects: Literal[False] = False,
    skip_json_loads: bool = False,
    logging: Literal[False] = False,
    json_fd: object | None = None,
    chunk_length: int = 0,
    stream_stable: bool = False,
    strict: bool = False,
    schema: object | None = None,
    schema_repair_mode: Literal["standard", "salvage"] = "standard",
    **json_dumps_args: object,
) -> str: ...
@overload
def repair_json(
    json_str: str = "",
    *,
    return_objects: Literal[True],
    skip_json_loads: bool = False,
    logging: Literal[False] = False,
    json_fd: object | None = None,
    chunk_length: int = 0,
    stream_stable: bool = False,
    strict: bool = False,
    schema: object | None = None,
    schema_repair_mode: Literal["standard", "salvage"] = "standard",
    **json_dumps_args: object,
) -> JSONRepairValue: ...
@overload
def repair_json(
    json_str: str = "",
    *,
    return_objects: bool = False,
    skip_json_loads: bool = False,
    logging: Literal[True],
    json_fd: object | None = None,
    chunk_length: int = 0,
    stream_stable: bool = False,
    strict: bool = False,
    schema: object | None = None,
    schema_repair_mode: Literal["standard", "salvage"] = "standard",
    **json_dumps_args: object,
) -> tuple[JSONRepairValue, list[dict[str, str]]]: ...
def loads(s: str, *args: object, **kwargs: object) -> JSONRepairValue: ...
