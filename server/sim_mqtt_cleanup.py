"""Release Paho 2 wakeup sockets only after the network loop has stopped."""


def reset_stopped_client(client):
    thread = getattr(client, '_thread', None)
    if thread is not None and thread.is_alive():
        raise RuntimeError('simulation network thread is still running')
    # Paho's public loop_stop() does not close this socket pair. Its own
    # finalizer invokes _reset_sockets, but callback cycles can defer that
    # finalizer to interpreter shutdown. Do not call while the loop is alive.
    reset = getattr(client, '_reset_sockets', None)
    if callable(reset):
        reset()
