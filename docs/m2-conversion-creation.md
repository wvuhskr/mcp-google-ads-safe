# Secondary website conversion creation

In an MCP client connected to this server, `create_conversion_action(name, category,
customer_id=None)` returns a draft for `confirm_and_apply`. Only omitted/None customer uses
the configured default. Category is required and exactly one of DEFAULT, PURCHASE, SIGNUP,
SUBMIT_LEAD_FORM, CONTACT, BOOK_APPOINTMENT or REQUEST_QUOTE. Names are preserved exactly,
nonblank, at most 128 characters and 255 UTF-8 bytes, with no controls or blocked terms.
Existing owner action names, including removed actions and case-insensitive matches, refuse.

The original request explicitly sets WEBPAGE, ENABLED, primary_for_goal=false,
ONE_PER_CLICK, a 30-day click window and a 1-day view window. These are local fixed choices,
not universal Google defaults. ENABLED allows recording when tracking sends conversions;
creation installs no tags and uploads no events. Value and attribution settings remain
provider-managed, with no invented revenue or attribution defaults. Both settings groups
are read and retained for existing actions. The new action's first saved settings must
match the later inventory; unreadable settings refuse verification. The reference project's
UPLOAD_CLICKS tool is intentionally not copied: this tool creates website outcomes.

Writes, selected customer and resolved owner's read/write permission, and
GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT=true are required before safety reads. That feature
flag is off by default; malformed values refuse. The resolved conversion customer is used
for the single ConversionActionService create in the existing atomic GoogleAdsService
request. No separate demotion or goal write is sent.

Every draft and confirmation walks the configured login manager's complete accessible tree,
including unrelated and otherwise unallowlisted accounts. This read-only control scan has
no depth, account, page or public pagination cap. All nodes' identity, manager status and
tracking owners must reconcile; hidden, canceled, inaccessible, cyclic or incomplete scope
refuses. Shared descendants are allowed. Every customer tracking to the conversion owner
must be write-authorized. The owner must be inside the tree and route to itself. This proves
only that manager tree, not invisible external customers.

The preview includes every affected account, all campaigns including paused campaigns,
customer/campaign goals, campaign goal configuration and custom goal usage. Existing matching
category+WEBSITE goals are reused unchanged. Absent customer goals are predicted true only
when no same-category goal is false; otherwise creation refuses because official default
descriptions differ. Absent campaign goals are false if another same-category campaign goal
is false, otherwise true. Removed campaigns are retained as historical evidence but excluded
from predicted creation. Missing references, configurations or unknown enums/flags refuse.

A campaign using a custom goal containing this action can still bid on it regardless of
primary status. Before creation, membership is empty because the action does not exist;
that is not a claimed provider query result. All current custom goals and their action
references are inventoried. There are no custom-goal writes or automatic repairs.

After a successful response, exactly one positive same-owner action identity must resolve
before any saved reads. The exact action must match all requested fields plus WEBSITE
origin and owner. A fresh full control scan must agree with that exact read and retain all
existing content unchanged, allowing only the expected new action and predicted goals.
Missing data, extra changes, new custom membership or later action-content races produce
an applied-but-unverified result and consume the draft. Unknown outcomes do not retry.
Validation-only responses cannot prove a saved action or automatic goal effects.

`get_conversion_actions` remains the existing selected-account, read-only, allowlisted,
paginated nonremoved configuration read. It is not used as the safety inventory. In the
same MCP client, discover existing configuration with that tool or `run_gaql`; neither
creates an action. This creation slice has offline fake-response and real v25 message
coverage only. No Google account, validation or live mutation call was made for this slice.
