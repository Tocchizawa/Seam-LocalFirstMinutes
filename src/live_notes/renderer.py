def render_markdown(topics: list[dict]) -> str:
    legacy_labels = {
        "context": "背景", "point": "論点", "proposal": "提案", "concern": "懸念",
        "decision": "決定", "question": "未解決", "action": "アクション",
    }
    lines = []
    for topic in sorted(topics, key=lambda x: (x["position"], x["id"])):
        if topic.get("deleted_at") or topic.get("archived_at"):
            continue
        lines.extend(["## " + topic["title"].replace("\n", " "), ""])
        for block in sorted(
            topic.get("blocks", []), key=lambda x: (x["position"], x["id"])
        ):
            if not block.get("deleted_at"):
                content = block["content_md"]
                # Preserve the established v1 export while rendering v2 roles as prose.
                if not block.get("semantic_v2") and block.get("semantic_role", "note") == "note":
                    content = f"**{legacy_labels.get(block['kind'], 'メモ')}:** {content}"
                lines.extend([content, ""])
    return "\n".join(lines).rstrip() + ("\n" if lines else "")
