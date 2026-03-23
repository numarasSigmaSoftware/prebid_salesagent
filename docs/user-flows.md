# User Flows

This document maps the main user and system flows currently implemented in the Prebid Sales Agent codebase.

## Actors

- Publisher admin: signs up, configures a tenant, manages products, advertisers, settings, and approvals in the Admin UI
- Buyer or buyer agent: discovers inventory and creates or manages media buys over MCP, REST, or A2A
- Seller system: resolves auth and tenant context, executes shared business logic, and routes approval-dependent work into workflows
- Human reviewer: approves workflow steps and, when required, unblocks downstream media buy execution

## Product Surfaces

- Public web: signup and onboarding
- Admin UI: tenant dashboard, settings, products, advertisers, workflows, inventory, users
- MCP: tool-based access for discovery and transaction flows
- REST API: `/api/v1/*` transport wrapper over shared tool logic
- A2A: agent card discovery plus task/message based invocation

## What Must Exist Before MCP Can Actually Sell Ads

The buyer-facing MCP flow depends on a publisher-side setup chain. In practice, the system is sell-ready only when these prerequisites are in place.

Required setup chain:

1. Tenant exists and auth works
2. Ad server is configured
3. For GAM tenants, inventory has been synced
4. Authorized properties and property tags exist as targeting scope
5. At least one advertiser principal exists with an access token
6. At least one product exists with formats, pricing options, and targeting or inventory mappings
7. Approval policy is configured enough for media buys to auto-approve or route cleanly to human review

Grounded in [setup_checklist_service.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/services/setup_checklist_service.py), [products.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/products.py), [gam.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/gam.py), [inventory.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/inventory.py), [authorized_properties.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/authorized_properties.py), and [principals.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/principals.py).

```mermaid
flowchart TD
    A["Tenant created"] --> B["Configure ad server"]
    B --> C{"Using GAM?"}
    C -- "Yes" --> D["Connect GAM credentials and network settings"]
    D --> E["Run inventory sync"]
    E --> F["Inventory available for product mapping"]
    C -- "No" --> G["Use adapter-specific product setup"]
    F --> H["Add authorized properties and tags"]
    G --> H
    H --> I["Create advertiser principal and token"]
    I --> J["Create product with pricing, formats, and targeting"]
    J --> K["Buyer can discover products via MCP"]
    K --> L["Buyer can create media buys against valid products"]
```

## Flow 1: Publisher Self-Service Signup

Grounded in [public.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/public.py) and [auth.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/auth.py).

```mermaid
flowchart TD
    A["Visitor lands on /signup"] --> B{"Already authenticated?"}
    B -- "Yes, has tenant" --> C["Redirect to tenant dashboard"]
    B -- "Yes, super admin" --> D["Redirect to core index"]
    B -- "No" --> E["Start signup flow"]
    E --> F["Set signup session state"]
    F --> G["Redirect to Google/OIDC auth"]
    G --> H["Return to signup onboarding"]
    H --> I["Submit publisher name and adapter choice"]
    I --> J["Provision tenant, adapter config, currency limit, admin user"]
    J --> K["Redirect to signup complete / tenant entry point"]
```

Notes:

- Signup is only allowed on the main domain, not tenant domains.
- Provisioning creates a tenant, adapter config, default budget/currency limits, and an admin user in one flow.

## Flow 2: Admin UI Login and Tenant Access

Grounded in [auth.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/auth.py), [app.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/app.py), and [tenants.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/tenants.py).

```mermaid
flowchart TD
    A["User hits /login or tenant login URL"] --> B["Detect tenant from host, subdomain, or virtual host"]
    B --> C{"Tenant-specific OIDC enabled?"}
    C -- "Yes" --> D["Redirect to tenant OIDC login"]
    C -- "No" --> E{"Global OAuth configured?"}
    E -- "Yes" --> F["Redirect to global OAuth"]
    E -- "No / test mode" --> G["Render login page"]
    D --> H["OAuth callback resolves session"]
    F --> H
    G --> H
    H --> I{"Has tenant context?"}
    I -- "Yes" --> J["Redirect to /tenant/<tenant_id> dashboard"]
    I -- "No" --> K["Redirect to tenant selection or core index"]
```

Key destinations after login:

- Tenant dashboard: metrics, recent media buys, setup checklist
- Tenant settings: adapter config, integrations, advertisers, inventory state
- Products and principals: operational setup
- Workflows: approval and audit visibility

## Flow 3: Admin Tenant Setup and Operation

Grounded in [tenants.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/tenants.py), [products.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/products.py), [principals.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/principals.py), and [workflows.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/workflows.py).

```mermaid
flowchart TD
    A["Admin opens tenant dashboard"] --> B["Review setup checklist and metrics"]
    B --> C["Configure settings and adapter"]
    C --> D["Create products / inventory mappings"]
    C --> E["Create advertisers (principals) and tokens"]
    D --> F["Buyer-facing discovery becomes available"]
    E --> F
    F --> G["Incoming media buys and creatives create operational load"]
    G --> H["Admin monitors workflows, activity, and media buys"]
    H --> I{"Manual approval needed?"}
    I -- "Yes" --> J["Review workflow step"]
    J --> K["Approve or reject"]
    I -- "No" --> L["System proceeds automatically"]
```

## Flow 3A: Configure Google Ad Manager

Grounded in [gam.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/gam.py) and [tenants.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/tenants.py).

```mermaid
flowchart TD
    A["Admin opens tenant settings"] --> B["Choose Google Ad Manager as adapter"]
    B --> C["Enter auth method, network code, trafficker, currency, templates"]
    C --> D["Save GAM config"]
    D --> E["Persist AdapterConfig and set tenant.ad_server"]
    E --> F["Auto-create currency limit for GAM network currency when missing"]
    F --> G["Tenant is now GAM-configured"]
```

Why it matters:

- Without GAM configuration, inventory sync cannot start.
- Without GAM network and auth data, product mappings to real GAM inventory cannot be validated.

## Flow 3B: Sync GAM Inventory

Grounded in [inventory.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/inventory.py), [background_sync_service.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/services/background_sync_service.py), and [gam_inventory_service.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/services/gam_inventory_service.py).

```mermaid
flowchart TD
    A["Admin triggers /api/tenant/<tenant_id>/inventory/sync"] --> B["Validate tenant exists and adapter is GAM"]
    B --> C["Validate GAM network config exists"]
    C --> D["Start background sync job"]
    D --> E["Discover ad units, placements, labels, targeting from GAM"]
    E --> F["Stream inventory into GAMInventory table"]
    F --> G["Update sync status and cached inventory views"]
    G --> H["Product form can now map products to synced inventory"]
```

Why it matters:

- GAM product creation can validate ad unit and placement IDs against synced inventory.
- The setup checklist treats inventory sync as a first-class prerequisite for GAM tenants.

## Flow 3C: Add Authorized Properties and Tags

Grounded in [authorized_properties.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/authorized_properties.py) and [products.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/products.py).

```mermaid
flowchart TD
    A["Admin creates or uploads authorized properties"] --> B["Store property type, name, publisher domain, identifiers, tags"]
    B --> C["Property verification runs or remains pending"]
    C --> D["Property tags become selectable in product form"]
    D --> E["Products can scope inventory by property IDs or tags"]
```

Why it matters:

- Products rely on authorized properties and property tags to define where inventory may be sold.
- Product save paths validate selected tags and property IDs against authorized property records.

## Flow 3D: Create Advertiser Principal and Token

Grounded in [principals.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/principals.py).

```mermaid
flowchart TD
    A["Admin opens advertisers / principals"] --> B["Create advertiser principal"]
    B --> C["System generates principal_id and access_token"]
    C --> D["Optional adapter mapping added, like GAM advertiser_id"]
    D --> E["Principal saved for tenant"]
    E --> F["Buyer can authenticate MCP calls with x-adcp-auth token"]
```

Why it matters:

- No principal means no buyer token.
- No buyer token means transactional MCP calls like `create_media_buy` cannot succeed.

## Flow 3E: Add a Product That MCP Can Sell

Grounded in [products.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/products.py).

```mermaid
flowchart TD
    A["Admin opens add product form"] --> B["Load formats, authorized properties, tags, principals, currencies"]
    B --> C["Admin enters product metadata"]
    C --> D["Select formats"]
    D --> E["Define pricing options"]
    E --> F["Choose property scope or full property selection"]
    F --> G{"GAM tenant?"}
    G -- "Yes" --> H["Map product to synced ad units, placements, or custom targeting"]
    G -- "No" --> I["Use non-GAM implementation config"]
    H --> J["Persist product, pricing options, and inventory mappings"]
    I --> J
    J --> K["Product becomes eligible for get_products discovery"]
```

What the product flow enforces:

- Product name is required
- At least one pricing option is required
- Formats are validated against the creative agent when available
- Property IDs and tags are validated against authorized property data
- For GAM, mapped ad units and placements are checked against synced inventory

## Flow 3F: Publisher Readiness to Sell via MCP

This is the full operational readiness flow that turns configuration into sellable inventory.

```mermaid
flowchart TD
    A["Publisher admin signs in"] --> B["Configure adapter"]
    B --> C{"GAM?"}
    C -- "Yes" --> D["Connect GAM and sync inventory"]
    C -- "No" --> E["Proceed with adapter-specific config"]
    D --> F["Create authorized properties and tags"]
    E --> F
    F --> G["Create advertiser principal and token"]
    G --> H["Create one or more products"]
    H --> I["Buyer authenticates with principal token"]
    I --> J["MCP get_products returns valid sellable offers"]
    J --> K["MCP create_media_buy can place demand against those offers"]
```

## Flow 4: Buyer Discovery to Media Buy via MCP or REST

Grounded in [main.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/core/main.py), [api_v1.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/routes/api_v1.py), and [test_mcp_tool_roundtrip_minimal.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/tests/integration/test_mcp_tool_roundtrip_minimal.py).

```mermaid
flowchart TD
    A["Buyer client calls get_products"] --> B["Transport resolves identity and tenant"]
    B --> C["Shared get_products implementation"]
    C --> D["Return eligible products"]
    D --> E["Buyer selects product and pricing option"]
    E --> F["Buyer calls create_media_buy"]
    F --> G["Transport enforces auth for transactional call"]
    G --> H["Shared create_media_buy implementation"]
    H --> I{"Auto-approvable?"}
    I -- "Yes" --> J["Create or schedule media buy in adapter"]
    I -- "No" --> K["Create pending approval workflow"]
    J --> L["Buyer can query status or delivery"]
    K --> M["Human approval path"]
```

Discovery vs transaction:

- Auth-optional discovery: capabilities, creative formats, authorized properties, and in some cases products
- Auth-required transaction: create media buy, update media buy, get delivery, creative sync/listing

Operational note:

- `get_products` only becomes commercially meaningful after the setup flows above are complete.
- In other words, MCP does not create sellable inventory by itself; it exposes inventory and rules the publisher has already configured in Admin UI.

## Flow 5: Buyer Invocation via A2A

Grounded in [adcp_a2a_server.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/a2a_server/adcp_a2a_server.py), [README.md](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/a2a_server/README.md), and [test_a2a_endpoints_working.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/tests/e2e/test_a2a_endpoints_working.py).

```mermaid
flowchart TD
    A["Buyer agent fetches agent card"] --> B["Discover skills and A2A endpoint"]
    B --> C["Send natural language or explicit skill task"]
    C --> D["A2A transport resolves identity once"]
    D --> E["Build tool context and call shared raw tool"]
    E --> F{"Discovery skill or auth-required skill?"}
    F -- "Discovery" --> G["Run auth-optional path"]
    F -- "Transactional" --> H["Require valid token"]
    G --> I["Return task/result payload"]
    H --> I
    I --> J{"Long-running or approval dependent?"}
    J -- "Yes" --> K["Task lifecycle continues with polling/status"]
    J -- "No" --> L["Immediate result returned"]
```

## Flow 6: Human Approval for Pending Media Buys

Grounded in [workflows.py](/Users/nicolas.umaras/Documents/GitHub/prebid_salesagent/src/admin/blueprints/workflows.py).

```mermaid
flowchart TD
    A["System creates workflow step with pending_approval"] --> B["Admin opens workflows page"]
    B --> C["Open review page for step"]
    C --> D["Inspect request, context, principal, audit trail"]
    D --> E{"Approve?"}
    E -- "No" --> F["Leave pending or reject through workflow action"]
    E -- "Yes" --> G["Workflow status updated to approved"]
    G --> H{"Creatives already approved?"}
    H -- "No" --> I["Media buy waits in pending_creatives"]
    H -- "Yes" --> J["Execute approved media buy in adapter"]
    J --> K["Set media buy to scheduled and record approver"]
```

## Cross-Cutting Flow Shape

This is the architectural pattern repeated across MCP, REST, and A2A.

```mermaid
flowchart LR
    A["Client"] --> B["Transport boundary"]
    B --> C["Resolve identity"]
    C --> D["Shared _impl / raw tool logic"]
    D --> E["Repositories / services / adapters"]
    E --> F["Transport-specific response"]
```

## Suggested Next Cuts

If we want to go deeper, the next useful diagrams would be:

- Creative lifecycle from sync to approval to assignment
- Tenant setup checklist as a state machine
- Admin information architecture by role: super admin vs tenant admin vs advertiser
- End-to-end media buy state machine from draft to active to delivery reporting
