"""Grade received audio in the answer's own playout window, not earlier cues."""


def received_speech_frames(events, audible_at, caption_at):
    speaking, started = False, None
    for event in events:
        if event.get('_at', 0) > caption_at:
            break
        if event.get('type') == 'state':
            current = bool(event.get('speaking'))
            if current and not speaking:
                started = event['_at']
            speaking = current
    if started is None:
        return 0
    # A small RTP tail may arrive after the final transcript on the control WS.
    return sum(started <= timestamp <= caption_at + .5 for timestamp in audible_at)
