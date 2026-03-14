import unittest

from core.control import PIConfig, PIController


class PIControllerRetargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = PIConfig(
            dt=0.1,
            u_min=0.0,
            u_max=10.0,
            deadband_kpa=0.0,
            u_ff=0.0,
            kp_low=0.5,
            ki_low=0.2,
            kp_mid=0.25,
            ki_mid=0.1,
            kp_high=0.1,
            ki_high=0.05,
        )

    def test_retarget_clears_integrator_and_updates_zone(self) -> None:
        pi = PIController(self.cfg)
        pi.set_zone_from_sp(zone_sp_kpa=20.0, error_now=0.0)
        pi.step(sp_kpa=20.0, p_kpa=0.0, dt=0.1)
        self.assertGreater(pi.I, 0.0)

        pi.retarget(sp_kpa=120.0, p_kpa=55.0)

        self.assertEqual(pi.I, 0.0)
        self.assertEqual(pi.zone_sp_active, 120.0)
        self.assertEqual(pi.kp_active, self.cfg.kp_high)
        self.assertEqual(pi.ki_active, self.cfg.ki_high)
        self.assertEqual(pi.last_p, 55.0)
        self.assertEqual(pi._p_filt, 55.0)

    def test_step_after_retarget_does_not_use_previous_integral(self) -> None:
        pi = PIController(self.cfg)
        pi.set_zone_from_sp(zone_sp_kpa=20.0, error_now=0.0)

        for _ in range(5):
            pi.step(sp_kpa=20.0, p_kpa=0.0, dt=0.1)

        self.assertGreater(pi.I, 0.0)

        pi.retarget(sp_kpa=60.0, p_kpa=50.0)
        u = pi.step(sp_kpa=60.0, p_kpa=50.0, dt=0.1)

        expected_i = self.cfg.ki_mid * 10.0 * 0.1
        expected_u = (self.cfg.kp_mid * 10.0) + expected_i
        self.assertAlmostEqual(pi.I, expected_i, places=9)
        self.assertAlmostEqual(u, expected_u, places=9)


if __name__ == "__main__":
    unittest.main()
