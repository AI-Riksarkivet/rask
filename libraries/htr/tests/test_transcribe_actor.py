def test_transcribe_actor_signature():
    from htr.actors.transcription import TranscribeActor

    assert "model" in TranscribeActor.__init__.__code__.co_varnames
    assert "max_batch" in TranscribeActor.__init__.__code__.co_varnames
    assert "dtype" in TranscribeActor.__init__.__code__.co_varnames
