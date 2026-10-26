"""Creates or updates one WhatsApp message template in End User Messaging Social (run by the
`terraform_data.whatsapp_template` local-exec in whatsapp_templates.tf; neither the AWS provider nor
CloudFormation has a resource for these templates).

Environment: WABA_ID (waba-...), AWS_REGION, TEMPLATE_DEFINITION (JSON: name, language, category,
components). Uses the AWS CLI with the caller's credentials (AWS_PROFILE as for terraform), so it also
works with `aws login` profiles.

- Missing (name + language): create it. Meta then reviews it (PENDING -> APPROVED / REJECTED).
- Exists with the same text, buttons and category: nothing to do, so adopting a template created by
  hand never sends an APPROVED template back to review.
- Exists with different content: update it in place (Meta reviews it again).
Never deletes: Meta blocks a deleted template's name for 30 days.
DRY_RUN=1 only reports what it would do.
"""
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional


def aws(*args: str) -> Dict[str, Any]:
    cmd = ["aws", "socialmessaging", *args, "--region", os.environ["AWS_REGION"], "--output", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"aws {' '.join(args[:1])} failed: {res.stderr.strip()}")
    return json.loads(res.stdout or "{}")


def find(waba_id: str, name: str, language: str) -> Optional[Dict[str, Any]]:
    token = None
    while True:
        page = aws("list-whatsapp-message-templates", "--id", waba_id, *(["--next-token", token] if token else []))
        for t in page.get("templates", []):
            if t.get("templateName") == name and t.get("templateLanguage") == language:
                return t
        token = page.get("nextToken")
        if not token:
            return None


def content(components: List[Dict[str, Any]], category: str) -> Any:
    """What Meta reviews: text, header format, buttons. Examples and Meta's own extra fields are ignored."""
    def button(b: Dict[str, Any]):
        return (b.get("type"), b.get("text"), b.get("url"))
    return (str(category).upper(), sorted(
        (c.get("type"), c.get("format"), c.get("text"), tuple(button(b) for b in c.get("buttons") or []))
        for c in components))


def blob(data: Any) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, f)
    f.close()
    return f.name


def main() -> None:
    waba_id = os.environ["WABA_ID"]
    definition = json.loads(os.environ["TEMPLATE_DEFINITION"])
    name, language, category = definition["name"], definition["language"], definition["category"]

    dry_run = os.environ.get("DRY_RUN") == "1"
    existing = find(waba_id, name, language)
    if existing is None and dry_run:
        print(f"[dry run] would create WhatsApp template {name} ({language}).")
        return
    if existing is None:
        res = aws("create-whatsapp-message-template", "--id", waba_id,
                  "--template-definition", f"fileb://{blob(definition)}")
        print(f"Created WhatsApp template {name} ({language}): {res.get('templateStatus')}, "
              f"Meta ID {res.get('metaTemplateId')}.")
        return

    meta_id = existing["metaTemplateId"]
    current = json.loads(aws("get-whatsapp-message-template", "--id", waba_id,
                             "--meta-template-id", meta_id)["template"])
    if content(current.get("components") or [], current.get("category", "")) == content(definition["components"], category):
        print(f"WhatsApp template {name} ({language}) is up to date: {existing.get('templateStatus')}, Meta ID {meta_id}.")
        return
    if dry_run:
        print(f"[dry run] would update WhatsApp template {name} ({language}), Meta ID {meta_id}.")
        return
    aws("update-whatsapp-message-template", "--id", waba_id, "--meta-template-id", meta_id,
        "--template-category", category, "--template-components", f"fileb://{blob(definition['components'])}")
    print(f"Updated WhatsApp template {name} ({language}), Meta ID {meta_id}; Meta reviews it again.")


if __name__ == "__main__":
    main()
