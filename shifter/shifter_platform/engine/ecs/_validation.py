"""Validate native provisioner task inputs before choosing a transport."""


def _validate_start_ecs_task_args(range_id: int, user_id: int, command: str) -> None:
    """Validate _start_ecs_task inputs, raising TypeError/ValueError on bad input."""
    if range_id is None or not isinstance(range_id, int):
        raise TypeError("range_id must be an integer")
    if user_id is None or not isinstance(user_id, int):
        raise TypeError("user_id must be an integer")
    if range_id < 0:
        raise ValueError("range_id must be non-negative")
    if user_id < 0:
        raise ValueError("user_id must be non-negative")
    if command is None or not isinstance(command, str):
        raise TypeError("command must be a string")
    if not command.strip():
        raise ValueError("command must be a non-empty string")
