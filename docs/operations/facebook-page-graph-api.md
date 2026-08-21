# Facebook Page publishing through Meta Graph API

Hermes publishes new photo posts for `AI BizWeek｜SoloBiz AI 一人公司商業誌`
through Meta Graph API instead of automating Facebook's Page composer DOM. This
removes the unstable composer/actor/window checks while preserving exact Page
identity verification, one-time human approval, payload binding, durable
de-duplication, and post-publication read-back.

## Meta setup

1. Create or select a Meta developer app owned by the correct Business
   portfolio.
2. Request `pages_show_list`, `pages_read_engagement`, and
   `pages_manage_posts`. App Review and Business Verification may be required
   when the app moves beyond roles and assets owned by its operators.
3. Obtain a user token, then call
   `GET /me/accounts?fields=id,name,access_token,tasks`. Select the exact row
   whose name is `AI BizWeek｜SoloBiz AI 一人公司商業誌`, confirm its tasks include
   `CREATE_CONTENT`, and retain its numeric Page ID and Page access token.
4. For unattended production operation, replace the short-lived setup token
   with a Business System User token assigned only to this Page and the
   required Page permissions.

Never paste a Page token into Telegram, Grace, a Kanban card, a command-line
argument, a URL, or a repository file.

## Local configuration

Run the interactive setup script from the active Hermes checkout. The token and
optional App Secret are prompted with hidden input, so they do not enter shell
history:

```bash
python scripts/configure_facebook_page_graph.py --page-id YOUR_NUMERIC_PAGE_ID
```

The script writes owner-only `~/.hermes/.env` entries:

- `FACEBOOK_GRAPH_API_VERSION=v26.0`
- `FACEBOOK_PAGE_ID`
- `FACEBOOK_PAGE_NAME`
- `FACEBOOK_PAGE_URL`
- `FACEBOOK_PAGE_ACCESS_TOKEN`
- `FACEBOOK_APP_SECRET` (optional, enables `appsecret_proof`)

Restart the Hermes gateway after configuration, start a fresh Grace session,
and run `facebook_page_graph_status`. A successful result must show the exact
configured Page ID and Page name. The tool never returns the token.

## Controlled publish contract

Grace computes SHA-256 over the exact UTF-8 message and the exact image bytes,
then requests one-time approval for this structure:

```json
{
  "external_targets": ["https://www.facebook.com/solobizai"],
  "routing": {"task_type": "facebook_page_api_publish"},
  "facebook_page_post": {
    "action": "create_post",
    "page_url": "https://www.facebook.com/solobizai",
    "transport": "graph_api",
    "message_sha256": "<64 lowercase hex>",
    "image_sha256": "<64 lowercase hex>"
  }
}
```

`facebook_page_api_publish` routes to the dedicated ClawOps Facebook Page API
worker. `browser_publish` is rejected for this Page before a Kanban execution
card can be created, and no browser fallback is permitted.

After approval, `facebook_page_graph_publish` performs:

1. Page token read-only identity preflight.
2. Exact Page URL, message hash, and image hash comparison.
3. Durable `facebook/create` reservation before the Graph POST.
4. `POST /{page-id}/photos` with `published=true`, caption, and one local image.
5. Immediate persistence of `post_id` and `photo_id`.
6. `GET /{post-id}` read-back of message, attachment, creation time, and
   permalink, followed by the durable `verified` state.

The tool does not retry an ambiguous POST. Any existing `create_started`,
`created`, or `verified` effect blocks another publication until operators
reconcile the exact Page feed or known post ID.
