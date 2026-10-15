import os
import sys
import unittest


WEB_FLASK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WEB_FLASK_ROOT not in sys.path:
    sys.path.insert(0, WEB_FLASK_ROOT)


class UniqueTrackCounterTest(unittest.TestCase):
    def test_duplicate_track_and_class_is_counted_once(self):
        from utils.tracking_counter import UniqueTrackCounter

        counter = UniqueTrackCounter()

        self.assertTrue(counter.register(7, "crack"))
        self.assertFalse(counter.register(7, "crack"))
        self.assertEqual(
            counter.to_summary(),
            {
                "unique_detections": 1,
                "unique_detections_by_class": {"crack": 1},
            },
        )

    def test_different_tracks_are_counted_separately(self):
        from utils.tracking_counter import UniqueTrackCounter

        counter = UniqueTrackCounter()

        counter.register(7, "crack")
        counter.register(8, "crack")

        self.assertEqual(
            counter.to_summary(),
            {
                "unique_detections": 2,
                "unique_detections_by_class": {"crack": 2},
            },
        )

    def test_same_track_id_with_different_classes_matches_realtime_semantics(self):
        from utils.tracking_counter import UniqueTrackCounter

        counter = UniqueTrackCounter()

        counter.register(7, "crack")
        counter.register(7, "hotspot")

        self.assertEqual(
            counter.to_summary(),
            {
                "unique_detections": 2,
                "unique_detections_by_class": {"crack": 1, "hotspot": 1},
            },
        )

    def test_detection_without_track_id_is_not_counted(self):
        from utils.tracking_counter import UniqueTrackCounter

        counter = UniqueTrackCounter()

        self.assertFalse(counter.register(None, "crack"))
        self.assertEqual(
            counter.to_summary(),
            {
                "unique_detections": 0,
                "unique_detections_by_class": {},
            },
        )


if __name__ == "__main__":
    unittest.main()
