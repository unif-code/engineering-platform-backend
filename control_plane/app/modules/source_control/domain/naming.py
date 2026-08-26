import unicodedata

from control_plane.app.modules.source_control.domain.transitions import SourceControlError

_REQUIREMENT_TYPES = frozenset({"feat", "fix", "refactor", "chore"})
_MAX_SLUG_CODE_POINTS = 48


class InvalidBranchName(SourceControlError):
    pass


def build_task_branch_name(
    *,
    requirement_type: str,
    work_item_number: int,
    title: str,
) -> str:
    normalized_type = str(requirement_type).strip().lower()
    if normalized_type not in _REQUIREMENT_TYPES:
        raise InvalidBranchName("requirement type is not branch-safe")
    if work_item_number <= 0:
        raise InvalidBranchName("work item number must be positive")

    normalized_title = unicodedata.normalize("NFKC", title).lower()
    slug_characters: list[str] = []
    separator_pending = False
    for character in normalized_title:
        if character.isalnum():
            if separator_pending and slug_characters:
                slug_characters.append("-")
            slug_characters.append(character)
            separator_pending = False
        else:
            separator_pending = True

    slug = "".join(slug_characters)[:_MAX_SLUG_CODE_POINTS].strip("-")
    if not slug:
        slug = normalized_type
    return f"{normalized_type}/wi-{work_item_number}-{slug}"
