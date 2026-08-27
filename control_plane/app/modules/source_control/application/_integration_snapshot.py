from control_plane.app.modules.source_control.ports import GitLabMergeRequestSnapshot


def has_valid_merge_fact_shape(snapshot: GitLabMergeRequestSnapshot) -> bool:
    if snapshot.state == "merged":
        return snapshot.merge_commit_sha is not None and snapshot.merged_at is not None
    return all(
        value is None
        for value in (
            snapshot.merge_commit_sha,
            snapshot.merge_user_id,
            snapshot.merged_at,
        )
    )


__all__: list[str] = []
