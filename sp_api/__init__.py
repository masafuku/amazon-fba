"""Amazon Selling Partner API (SP-API) integration.

Deliberately independent of keepa_mcp/: this package is never imported by,
and never imports, keepa_mcp code, and makes no Keepa API calls at runtime.
Keepa is required for finding new product candidates (daily_scan.py); it is
NOT required for tracking an already-listed product's orders, fees, and FBA
inventory, which is what this package does. See the "Keepaからの独立性"
section of the design plan for the reasoning.
"""
