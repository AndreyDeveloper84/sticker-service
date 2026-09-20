"""Channel flow errors, in a leaf module so that services below
channel_order_flow (media / photo_gate) can raise them without an import
cycle. ``ChannelFlowError`` is re-exported from channel_order_flow."""


class ChannelFlowError(ValueError):
    pass
