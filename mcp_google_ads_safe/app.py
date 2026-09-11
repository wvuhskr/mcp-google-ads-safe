"""Keep domain failures explicit at registration without changing direct tool calls."""
import inspect
import json
from functools import wraps

try:
    from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError as _ToolError
except ModuleNotFoundError:
    # mcp 2.0 renamed FastMCP to MCPServer (same tool()/run() surface we use)
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError

from .rails import RailViolation, UnknownWriteOutcome


def _safety_error(error):
    if isinstance(error, UnknownWriteOutcome):
        data = {
            'code': error.code,
            'reason': 'The write may have landed; verify account state before retrying. '
                      'Do not retry this write blindly.',
            'request_id': error.request_id,
            'failure': error.failure,
        }
    else:
        data = {'code': error.code or 'RAIL_VIOLATION', 'reason': str(error)}
    # Provider details are normally JSON data. Never stringify arbitrary objects or
    # exception causes if a provider supplies an opaque detail instead.
    return _ToolError(json.dumps(data, default=lambda _: '<non-JSON provider detail>'))


class _SafetyServer(_Server):
    async def call_tool(self, name, arguments, *args, **kwargs):
        # MCP preparses JSON strings before strict field validation. Discovery must
        # validate the original container and optional strings before that conversion.
        if name == "discover_keywords":
            if (type(arguments.get("seed_keywords")) is not list
                    or any(type(seed) is not str for seed in arguments["seed_keywords"])
                    or any(arguments.get(key) is not None and type(arguments[key]) is not str
                           for key in ("customer_id", "page_token"))
                    or any(isinstance(arguments.get(key), str) and arguments[key].strip() == "null" for key in ("customer_id", "page_token"))):
                raise _safety_error(RailViolation("discovery requires an actual string list and "
                                                   "optional strings", code="BAD_INPUT"))
        if name == "get_policy_issues":
            if any(arguments.get(key) is not None and (
                type(arguments[key]) is not str or arguments[key].strip() == "null"
            ) for key in ("customer_id", "page_token")):
                raise _safety_error(RailViolation("policy reads require actual optional strings",
                                                 code="BAD_INPUT"))
        if name == "get_keyword_forecasts":
            if (type(arguments.get("keyword_texts")) is not list
                    or any(type(text) is not str for text in arguments["keyword_texts"])
                    or any(type(arguments.get(key)) is not str for key in
                           ("match_type", "forecast_start_date", "forecast_end_date"))
                    or type(arguments.get("max_cpc_bid_micros")) is not int
                    or (arguments.get("daily_budget_micros") is not None
                        and type(arguments["daily_budget_micros"]) is not int)
                    or (arguments.get("customer_id") is not None
                        and (type(arguments["customer_id"]) is not str
                             or arguments["customer_id"].strip() == "null"))):
                raise _safety_error(RailViolation("forecast requires actual string/list/integer "
                                                 "inputs without coercion", code="BAD_INPUT"))
        if name == 'remove_entity':
            if (type(arguments.get('entity_type')) is not str
                    or type(arguments.get('entity_id')) is not str
                    or any(arguments.get(key) is not None and (
                        type(arguments[key]) is not str
                        or arguments[key].strip() == 'null')
                           for key in ('customer_id', 'ad_group_id'))):
                raise _safety_error(RailViolation(
                    'remove_entity requires actual string IDs without coercion', code='BAD_INPUT'))
        if name == 'attach_shared_set':
            from .client import demand_gen_ad_id
            try:
                if set(arguments) - {'shared_set_id', 'campaign_id', 'customer_id'}:
                    raise RailViolation('unknown shared attachment argument', code='BAD_INPUT')
                demand_gen_ad_id(arguments.get('shared_set_id'))
                demand_gen_ad_id(arguments.get('campaign_id'))
                if 'customer_id' in arguments:
                    demand_gen_ad_id(arguments['customer_id'])
            except RailViolation as exc:
                raise _safety_error(exc) from None
        if name == 'add_to_shared_set':
            if (set(arguments) - {'shared_set_id', 'keywords', 'customer_id'}
                    or type(arguments.get('shared_set_id')) is not str
                    or ('customer_id' in arguments and type(arguments['customer_id']) is not str)):
                raise _safety_error(RailViolation('shared additions require original string IDs', code='BAD_INPUT'))
            from .client import demand_gen_ad_id, shared_negative_keywords
            try:
                demand_gen_ad_id(arguments['shared_set_id'])
                if 'customer_id' in arguments:
                    demand_gen_ad_id(arguments['customer_id'])
                shared_negative_keywords(arguments.get('keywords'))
            except RailViolation as exc:
                raise _safety_error(exc) from None
        if name == 'create_shared_negative_set':
            if (set(arguments) - {'name', 'customer_id'}
                    or type(arguments.get('name')) is not str
                    or ('customer_id' in arguments and
                        (type(arguments['customer_id']) is not str
                         or arguments['customer_id'].strip() == 'null'))):
                raise _safety_error(RailViolation(
                    'shared negative creation requires original string arguments', code='BAD_INPUT'))
        if name == 'create_portfolio_bidding_strategy':
            if (type(arguments.get('name')) is not str
                    or type(arguments.get('strategy_type')) is not str
                    or (arguments.get('customer_id') is not None
                        and type(arguments['customer_id']) is not str)
                    or any(isinstance(arguments.get(key), str)
                           and arguments[key].strip() == 'null'
                           for key in ('customer_id', 'target_cpa', 'target_roas'))):
                raise _safety_error(RailViolation(
                    'portfolio creation requires original string and target inputs without '
                    'coercing literal null', code='BAD_INPUT'))
        if name == 'create_pmax_campaign':
            strings = ('campaign_name', 'asset_group_name', 'business_name', 'final_url',
                       'landscape_image_asset_id', 'square_image_asset_id', 'logo_asset_id')
            lists = ('geo_target_ids', 'language_ids', 'headlines', 'long_headlines', 'descriptions')
            if (any(type(arguments.get(key)) is not str for key in strings)
                    or any(type(arguments.get(key)) is not list
                           or any(type(item) is not str for item in arguments[key]) for key in lists)
                    or type(arguments.get('contains_eu_political_advertising')) is not bool
                    or (arguments.get('customer_id') is not None
                        and (type(arguments['customer_id']) is not str or arguments['customer_id'].strip() == 'null'))
                    or any(type(arguments.get(key)) not in (str, int, float) for key in ('daily_budget', 'target_cpa'))):
                raise _safety_error(RailViolation('PMax requires original string/list/money/boolean inputs without coercion', code='BAD_INPUT'))
        if name == 'draft_demand_gen_ad':
            required = {'ad_group_id', 'headline', 'description', 'business_name',
                        'square_marketing_image_asset_id', 'logo_image_asset_id', 'final_url'}
            if (set(arguments) - (required | {'customer_id'})
                    or any(type(arguments.get(key)) is not str for key in required)
                    or ('customer_id' in arguments and
                        (type(arguments['customer_id']) is not str
                         or arguments['customer_id'].strip() == 'null'))):
                raise _safety_error(RailViolation(
                    'Demand Gen ad requires original string arguments without coercion',
                    code='BAD_INPUT'))
        if name == 'create_demand_gen_campaign':
            allowed = {'campaign_name', 'daily_budget', 'geo_target_ids', 'language_ids',
                       'contains_eu_political_advertising', 'customer_id'}
            if (set(arguments) - allowed
                    or type(arguments.get('campaign_name')) is not str
                    or type(arguments.get('geo_target_ids')) is not list
                    or any(type(item) is not str for item in arguments['geo_target_ids'])
                    or type(arguments.get('language_ids')) is not list
                    or any(type(item) is not str for item in arguments['language_ids'])
                    or type(arguments.get('contains_eu_political_advertising')) is not bool
                    or type(arguments.get('daily_budget')) not in (str, int, float)
                    or ('customer_id' in arguments and
                        (type(arguments['customer_id']) is not str
                         or arguments['customer_id'].strip() == 'null'))):
                raise _safety_error(RailViolation(
                    'Demand Gen requires original string/list/money/boolean inputs without coercion',
                    code='BAD_INPUT'))
        if name == 'create_asset_group':
            strings = ('campaign_id', 'asset_group_name', 'final_url',
                       'landscape_image_asset_id', 'square_image_asset_id')
            lists = ('headlines', 'long_headlines', 'descriptions')
            if (any(type(arguments.get(key)) is not str for key in strings)
                    or any(type(arguments.get(key)) is not list
                           or any(type(item) is not str for item in arguments[key]) for key in lists)
                    or (arguments.get('customer_id') is not None
                        and (type(arguments['customer_id']) is not str
                             or arguments['customer_id'].strip() == 'null'))):
                raise _safety_error(RailViolation(
                    'asset group creation requires original string and string-list inputs without coercion',
                    code='BAD_INPUT'))
        if name == 'update_asset_group':
            optional = ('customer_id', 'name', 'final_url')
            if (type(arguments.get('asset_group_id')) is not str
                    or any(arguments.get(key) is not None
                           and type(arguments[key]) is not str for key in optional)
                    or any(isinstance(arguments.get(key), str)
                           and arguments[key].strip() == 'null'
                           for key in ('asset_group_id', *optional))):
                raise _safety_error(RailViolation(
                    'asset group update requires original string inputs without coercion or literal null',
                    code='BAD_INPUT'))
        if name == 'add_asset_group_assets':
            assets = arguments.get('assets')
            if (type(arguments.get('asset_group_id')) is not str
                    or type(assets) is not list
                    or any(type(item) is not dict
                           or set(item) != {'asset_id', 'field_type'}
                           or any(type(item.get(key)) is not str
                                  for key in ('asset_id', 'field_type'))
                           for item in assets)
                    or ('customer_id' in arguments and arguments['customer_id'] is None)
                    or (arguments.get('customer_id') is not None
                        and type(arguments['customer_id']) is not str)
                    or any(isinstance(arguments.get(key), str)
                           and arguments[key].strip() == 'null'
                           for key in ('asset_group_id', 'customer_id'))):
                raise _safety_error(RailViolation(
                    'asset-group additions require original string IDs and exact asset items',
                    code='BAD_INPUT'))
        if name == 'set_listing_group_filter':
            if (type(arguments.get('asset_group_id')) is not str
                    or type(arguments.get('product_item_ids')) is not list
                    or any(type(item) is not str for item in arguments['product_item_ids'])
                    or ('customer_id' in arguments and (type(arguments['customer_id']) is not str
                        or arguments['customer_id'] == 'null'))):
                raise _safety_error(RailViolation(
                    'listing filter requires original string IDs and string list; explicit null is invalid',
                    code='BAD_INPUT'))
        if name == 'remove_asset_group_asset':
            required = ('asset_group_id', 'asset_id', 'field_type')
            if (any(type(arguments.get(key)) is not str for key in required)
                    or ('customer_id' in arguments and arguments['customer_id'] is None)
                    or (arguments.get('customer_id') is not None
                        and type(arguments['customer_id']) is not str)
                    or any(isinstance(arguments.get(key), str)
                           and arguments[key].strip() == 'null'
                           for key in (*required, 'customer_id'))):
                raise _safety_error(RailViolation(
                    'asset-group removal requires original string IDs and role; explicit null is invalid',
                    code='BAD_INPUT'))
        return await super().call_tool(name, arguments, *args, **kwargs)

    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)

        def decorator(fn):
            @wraps(fn)
            async def registered(*args, **kwargs):
                try:
                    result = fn(*args, **kwargs)
                    return await result if inspect.isawaitable(result) else result
                except (RailViolation, UnknownWriteOutcome) as error:
                    raise _safety_error(error) from error
                except Exception as error:
                    # SDK 1 includes unexpected exception text by default. Keep it
                    # private under both supported SDKs; never stringify the cause.
                    raise _ToolError('Unexpected tool failure') from error

            register(registered)
            return fn

        return decorator


mcp = _SafetyServer('mcp-google-ads-safe')
