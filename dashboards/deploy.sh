set -eo pipefail

# Deploy o11yag.json to Dynatrace as a dashboard document.
#
# Uses the Document API directly with the same credentials the Dynatrace MCP
# plugin uses ($DT_ENVIRONMENT / $DT_PLATFORM_TOKEN). No dtctl needed: the
# tenant's MCP gateway exposes only read tools, so there is no MCP path for
# creating a document, but the REST API underneath it is perfectly writable.
#
# Usage:
#   ./deploy.sh                 create a new dashboard
#   ./deploy.sh <document-id>   update that dashboard in place
#
# The document id of the current deployment is kept in .dashboard-id so a
# re-run updates rather than piling up duplicates.

# Response body goes to the sandbox-writable temp dir when there is one;
# /tmp is not always writable (Claude Code's bash sandbox, for one).
OUT="${TMPDIR:-/tmp}/o11yag-deploy.json"

FILE="o11yag.json"
NAME="o11yag — agent operations"
ID_FILE=".dashboard-id"
ID="${1:-$( [ -f "$ID_FILE" ] && cat "$ID_FILE" )}"
ID="$(echo "$ID" | tr -d '[:space:]')"   # an empty/whitespace id file must not silently mean "create a new one"

if [ -z "$DT_ENVIRONMENT" ] || [ -z "$DT_PLATFORM_TOKEN" ]; then
  echo "DT_ENVIRONMENT and DT_PLATFORM_TOKEN must be set (same vars the Dynatrace plugin uses)." >&2
  exit 1
fi
[ -f "$FILE" ] || { echo "$FILE not found" >&2; exit 1; }
python3 -c "import json,sys; json.load(open('$FILE'))" || { echo "$FILE is not valid JSON" >&2; exit 1; }

if [ -n "$ID" ]; then
  # An update must send the version it is replacing, so read it first. This is
  # the same reason the dashboard skill insists on downloading before updating:
  # without it you silently overwrite edits someone made in the UI.
  # NOTE the /metadata suffix. A plain GET on the document returns a multipart
  # body (metadata part + content part), which is not JSON and will not parse.
  ver="$(curl -sS -H "Authorization: Bearer $DT_PLATFORM_TOKEN" \
          "$DT_ENVIRONMENT/platform/document/v1/documents/$ID/metadata" \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])")"
  if [ -z "$ver" ]; then echo "Could not read current version of $ID" >&2; exit 1; fi
  echo "Updating $ID (version $ver -> $((ver+1)))"
  code="$(curl -sS -o "$OUT" -w '%{http_code}' \
    -X PATCH "$DT_ENVIRONMENT/platform/document/v1/documents/$ID?optimistic-locking-version=$ver" \
    -H "Authorization: Bearer $DT_PLATFORM_TOKEN" \
    -F "name=$NAME" -F "content=@$FILE;type=application/json")"
else
  echo "Creating new dashboard"
  code="$(curl -sS -o "$OUT" -w '%{http_code}' \
    -X POST "$DT_ENVIRONMENT/platform/document/v1/documents" \
    -H "Authorization: Bearer $DT_PLATFORM_TOKEN" \
    -F "name=$NAME" -F "type=dashboard" -F "content=@$FILE;type=application/json")"
fi

if [ "$code" != "200" ] && [ "$code" != "201" ]; then
  echo "Failed (HTTP $code):" >&2
  cat "$OUT" >&2; echo >&2
  exit 1
fi

# POST returns the metadata flat; PATCH nests it under "documentMetadata".
new_id="$(OUT="$OUT" python3 -c '
import json, os
d = json.load(open(os.environ["OUT"]))
print(d.get("id") or d["documentMetadata"]["id"])
')"
if [ -z "$new_id" ]; then
  echo "Deployed, but could not parse the document id from the response." >&2
  echo "Not touching $ID_FILE — a blank id here makes the next run create a duplicate." >&2
  exit 1
fi
echo "$new_id" > "$ID_FILE"
echo "OK  →  $DT_ENVIRONMENT/ui/apps/dynatrace.dashboards/dashboard/$new_id"
