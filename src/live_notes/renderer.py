LABELS = {
    "context": "背景",
    "point": "論点",
    "proposal": "提案",
    "concern": "懸念",
    "decision": "決定",
    "question": "未解決",
    "action": "アクション",
}


def render_markdown(topics: list[dict]) -> str:
    lines = []
    for topic in sorted(topics, key=lambda x: (x["position"], x["id"])):
        if topic.get("deleted_at"):
            continue
        lines.extend(["## " + topic["title"].replace("\n", " "), ""])
        for block in sorted(
            topic.get("blocks", []), key=lambda x: (x["position"], x["id"])
        ):
            if not block.get("deleted_at"):
                lines.extend(
                    [f"**{LABELS[block['kind']]}:** {block['content_md']}", ""]
                )
    return "\n".join(lines).rstrip() + ("\n" if lines else "")
