import configparser
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_throttled():
    spec = importlib.util.spec_from_file_location('throttled_under_test', ROOT / 'throttled.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.args = SimpleNamespace(log=None, debug=False, config='/tmp/throttled.conf', monitor=None)
    module.log_history.clear()
    module.power['source'] = 'AC'
    module.power['method'] = 'dbus'
    return module


def make_config(sections):
    config = configparser.ConfigParser()
    for section, options in sections.items():
        config.add_section(section)
        for option, value in options.items():
            config.set(section, option, str(value))
    return config


class StopAfterWait:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.stopped = True


class Round2FixTests(unittest.TestCase):
    def write_config(self, text):
        tmp = tempfile.NamedTemporaryFile('w', suffix='.conf', delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_undervolt_decode_handles_the_sign_boundary(self):
        throttled = load_throttled()

        for offset_mv in (0, -100, -125, -999, -1000):
            msr = throttled.calc_undervolt_msr('CORE', offset_mv)
            self.assertEqual(throttled.calc_undervolt_mv(msr & 0xFFFFFFFF), offset_mv)

    def test_trip_offset_is_clamped_to_the_six_bit_msr_field(self):
        throttled = load_throttled()
        config = make_config(
            {
                'AC': {'Update_Rate_s': '5', 'Trip_Temp_C': '40'},
                'BATTERY': {'Update_Rate_s': '30', 'Trip_Temp_C': '40'},
            }
        )

        with mock.patch.object(throttled, 'get_critical_temp', return_value=105):
            with mock.patch.object(throttled, 'log'):
                regs = throttled.calc_reg_values(
                    {'feature_programmable_temperature_target': 1, 'feature_programmable_tdp_limit': 0},
                    config,
                )

        self.assertEqual(regs['AC']['MSR_TEMPERATURE_TARGET'], 63 << 24)
        self.assertEqual(regs['BATTERY']['MSR_TEMPERATURE_TARGET'], 63 << 24)

    def test_icc_max_encoder_rejects_values_that_overflow_ten_bits(self):
        throttled = load_throttled()

        self.assertEqual(throttled.calc_icc_max_msr('CORE', 0x3FF / 4) & 0x3FF, 0x3FF)
        with self.assertRaises(AssertionError):
            throttled.calc_icc_max_msr('CORE', 256)

    def test_load_config_survives_a_partial_undervolt_section(self):
        throttled = load_throttled()
        throttled.args.config = self.write_config(
            '[GENERAL]\nEnabled: True\n[AC]\nUpdate_Rate_s: 5\n[UNDERVOLT]\nCORE: -50\n'
        )

        config = throttled.load_config()

        self.assertEqual(config.getfloat('UNDERVOLT', 'CORE'), -50)
        self.assertEqual(config.getfloat('UNDERVOLT', 'CACHE', fallback=0.0), 0.0)

    def test_load_config_rejects_iccmax_values_that_overflow_the_field(self):
        throttled = load_throttled()
        throttled.args.config = self.write_config(
            '[GENERAL]\nEnabled: True\n[AC]\nUpdate_Rate_s: 5\n[ICCMAX.AC]\nCORE: 300\nGPU: 100\n'
        )

        with mock.patch.object(throttled, 'warning'):
            config = throttled.load_config()

        self.assertFalse(config.has_option('ICCMAX.AC', 'CORE'))
        self.assertEqual(config.getfloat('ICCMAX.AC', 'GPU'), 100)

    def test_resume_listener_checks_the_planes_each_key_actually_supports(self):
        throttled = load_throttled()

        self.assertFalse(throttled.should_listen_for_resume(make_config({'ICCMAX.AC': {'UNCORE': 200}})))
        self.assertTrue(throttled.should_listen_for_resume(make_config({'ICCMAX.AC': {'CORE': 200}})))
        self.assertTrue(throttled.should_listen_for_resume(make_config({'UNDERVOLT.AC': {'ANALOGIO': -50}})))

    def test_set_icc_max_honors_an_explicit_power_source(self):
        throttled = load_throttled()
        throttled.power['source'] = 'BATTERY'
        config = make_config({'ICCMAX.AC': {'CORE': 100}})

        with mock.patch.object(throttled, 'writemsr') as writemsr:
            with mock.patch.object(throttled, 'warning'):
                throttled.set_icc_max(config, source='AC')

        writemsr.assert_called_once_with('MSR_OC_MAILBOX', throttled.calc_icc_max_msr('CORE', 100))

    def test_power_flip_reapplies_undervolt_and_iccmax(self):
        throttled = load_throttled()
        throttled.power['source'] = 'BATTERY'
        throttled.power['method'] = 'polling'
        config = make_config(
            {
                'GENERAL': {'Autoreload': 'False'},
                'AC': {'Update_Rate_s': '5'},
                'BATTERY': {'Update_Rate_s': '30'},
            }
        )
        state = {'config': config, 'regs': {'AC': {}, 'BATTERY': {}}}
        calls = []

        with mock.patch.object(throttled, 'read_mchbar_base', return_value=0):
            with mock.patch.object(throttled, 'MMIO'):
                with mock.patch.object(throttled, 'is_on_battery', return_value=False):
                    with mock.patch.object(throttled, 'undervolt', lambda config, source=None: calls.append(('undervolt', source))):
                        with mock.patch.object(throttled, 'set_icc_max', lambda config, source=None: calls.append(('iccmax', source))):
                            with mock.patch.object(throttled, 'log'):
                                throttled.power_thread(state, StopAfterWait(), None)

        self.assertEqual(calls, [('undervolt', 'AC'), ('iccmax', 'AC')])
        self.assertEqual(throttled.power['source'], 'AC')

    def test_fatal_in_a_worker_thread_kills_the_whole_process(self):
        throttled = load_throttled()

        def run_fatal():
            try:
                throttled.fatal('boom')
            except SystemExit:
                # reached only because os._exit is mocked out below
                pass

        with mock.patch.object(throttled.os, '_exit') as os_exit:
            with mock.patch('sys.stderr', new=io.StringIO()):
                thread = Thread(target=run_fatal)
                thread.start()
                thread.join()

        os_exit.assert_called_once_with(1)

    def test_power_thread_crash_exits_the_process_for_systemd_to_restart(self):
        throttled = load_throttled()

        with mock.patch.object(throttled, '_power_thread', side_effect=RuntimeError('boom')):
            with mock.patch.object(throttled.os, '_exit') as os_exit:
                with mock.patch.object(throttled, 'warning'):
                    throttled.power_thread({}, None, None)

        os_exit.assert_called_once_with(1)


if __name__ == '__main__':
    unittest.main()
