"""Optional deterministic Archive/MT5 adapter. Importing it starts nothing.

The adapter never imports TradingAgents' research graph or invokes a model.
Only an authenticated, explicit human confirmation in Archive can dispatch an
already prepared local order. See README.md for activation and failure behavior.
"""
