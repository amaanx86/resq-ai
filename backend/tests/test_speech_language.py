"""SPEECH-1: text language detection (WhatsApp text messages and hi-IN re-labelling)."""
import pytest

from app.services.audio_service import audio_service, detect_language

# Demo transcripts from app/routers/analytics.py and tests/test_triage.py
URDU_CARDIAC = "Mere uncle ko dil ka daura par raha hai, seene me shadeed dard hai, please jaldi ambulance bhejein!"
URDU_CARDIAC_DEMO = "Mere uncle ko dil ka daura par raha hai, seene me shadeed dard hai, left arm me bhi dard ja raha hai!"
HINDI_TRAUMA = "Bhaiya highway par car accident ho gaya hai, sar se bahut khoon beh raha hai, jaldi ambulance bhejo!"
ENGLISH_STROKE = "My mother suddenly collapsed, her face is drooping on one side and she cannot speak clearly, please help!"
URDU_COLD = "Mujhe kal se halka sa zukam aur sar dard hai, koi tablet bata dein please."
HINDI_FEVER = "Kripya jaldi aaiye, beti ko teen din se tez bukhar hai aur ulti ho rahi hai."


@pytest.mark.parametrize("text,expected", [
    (URDU_CARDIAC, "Urdu"),
    (URDU_CARDIAC_DEMO, "Urdu"),
    (HINDI_TRAUMA, "Hindi"),
    (ENGLISH_STROKE, "English"),
    (URDU_COLD, "Urdu"),
    (HINDI_FEVER, "Hindi"),
    ("Ammi ki saans nahi aa rahi, madad karein", "Urdu"),
    ("Kripya turant aaiye, papa behosh ho gaye hain", "Hindi"),
    ("My son fell from the stairs and is bleeding from his head", "English"),
    ("help me please, chest pain", "English"),
])
def test_romanized_and_english(text, expected):
    language, confidence = detect_language(text)
    assert language == expected
    assert 0.5 <= confidence <= 0.99


@pytest.mark.parametrize("text,expected", [
    ("میرے ابو کو سینے میں شدید درد ہے، جلدی ایمبولینس بھیجیں", "Urdu"),
    ("मेरे पिताजी को छाती में दर्द है, कृपया जल्दी आइए", "Hindi"),
    # Urdu vocabulary in Devanagari (what Transcribe hi-IN returns for an Urdu speaker)
    ("मेरे चाचा को सीने में शदीद दर्द है, फौरन एम्बुलेंस भेजें", "Urdu"),
])
def test_script_detection(text, expected):
    language, confidence = detect_language(text)
    assert language == expected
    assert confidence >= 0.6


@pytest.mark.parametrize("text", ["", "   ", "123 456", "!!!"])
def test_no_letters_is_english_with_zero_confidence(text):
    assert detect_language(text) == ("English", 0.0)


def test_dard_alone_no_longer_forces_urdu():
    """The old heuristic called anything containing 'dard' Urdu."""
    assert detect_language("Bhaiya, chhati me dard hai, kripya jaldi aaiye")[0] == "Hindi"


def test_deterministic_and_exposed_on_service():
    assert audio_service.detect_language(HINDI_TRAUMA) == detect_language(HINDI_TRAUMA)
    assert detect_language(URDU_CARDIAC) == detect_language(URDU_CARDIAC)
