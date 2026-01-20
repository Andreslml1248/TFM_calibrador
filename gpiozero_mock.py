"""
Mock de gpiozero.pins.lgpio para Windows.
Permite importar en Windows pero no hace nada real.
En Raspberry Pi se usará el módulo real.
"""

import sys
import platform

if platform.system() == "Windows":
    # Mock simple para Windows
    class MockPinInfo:
        """Información del pin GPIO"""
        def __init__(self, name):
            self.name = name
            self.row = 0
            self.col = 0

    class MockPin:
        """Pin GPIO mock - compatible con gpiozero"""

        def __init__(self, spec):
            self.spec = spec
            self.state = 0
            self.frequency = 0
            self.value = 0
            self.frequency_hardware_pwm = False
            self.pull = None
            self.drive = None
            self.edges = None
            self.bounce = None
            self.info = MockPinInfo(f"GPIO{spec}")
            self._state = 0

        def __call__(self, *args, **kwargs):
            return self

        def output_with_state(self, state):
            """Simula output_with_state"""
            self._state = state
            return self

        def input(self):
            """Simula input"""
            return self

        def close(self):
            """Simula close"""
            pass

        def read(self):
            """Simula read"""
            return self._state

        def __getattr__(self, name):
            """Retorna 0 o None para atributos desconocidos"""
            return 0

    class MockFactory:
        """Factory mock para GPIO en Windows"""

        def __init__(self):
            self.pins = {}
            self._reserved_pins = {}

        def pin(self, spec):
            if spec not in self.pins:
                self.pins[spec] = MockPin(spec)
            return self.pins[spec]

        def reserve_pins(self, requester, *pins):
            """Reservar pines (no hace nada en mock, pero es requerido por gpiozero)"""
            for pin in pins:
                if pin not in self._reserved_pins:
                    self._reserved_pins[pin] = []
                self._reserved_pins[pin].append(requester)

        def release_pins(self, requester, *pins):
            """Liberar pines reservados"""
            for pin in pins:
                if pin in self._reserved_pins:
                    if requester in self._reserved_pins[pin]:
                        self._reserved_pins[pin].remove(requester)

        def release_all(self, requester):
            """Liberar todos los pines del requester"""
            for pins in self._reserved_pins.values():
                if requester in pins:
                    pins.remove(requester)

        def close(self):
            """Cerrar la factory"""
            for pins in self.pins.values():
                pins.close()
            self.pins.clear()

    class LGPIOFactory(MockFactory):
        """Mock de LGPIOFactory para Windows"""
        pass

else:
    # En Linux/Raspberry Pi, importar el real
    try:
        from gpiozero.pins.lgpio import LGPIOFactory
    except ImportError:
        # Si no está disponible, usar el mock
        class MockPinInfo:
            """Información del pin GPIO"""
            def __init__(self, name):
                self.name = name
                self.row = 0
                self.col = 0

        class MockPin:
            def __init__(self, spec):
                self.spec = spec
                self.state = 0
                self.frequency = 0
                self.value = 0
                self.frequency_hardware_pwm = False
                self.pull = None
                self.drive = None
                self.edges = None
                self.bounce = None
                self.info = MockPinInfo(f"GPIO{spec}")
                self._state = 0

            def output_with_state(self, state):
                self._state = state
                return self

            def input(self):
                return self

            def close(self):
                pass

            def read(self):
                return self._state

            def __getattr__(self, name):
                return 0

        class MockFactory:
            def __init__(self):
                self.pins = {}
                self._reserved_pins = {}

            def pin(self, spec):
                if spec not in self.pins:
                    self.pins[spec] = MockPin(spec)
                return self.pins[spec]

            def reserve_pins(self, requester, *pins):
                """Reservar pines"""
                for pin in pins:
                    if pin not in self._reserved_pins:
                        self._reserved_pins[pin] = []
                    self._reserved_pins[pin].append(requester)

            def release_pins(self, requester, *pins):
                """Liberar pines reservados"""
                for pin in pins:
                    if pin in self._reserved_pins:
                        if requester in self._reserved_pins[pin]:
                            self._reserved_pins[pin].remove(requester)

            def release_all(self, requester):
                """Liberar todos los pines del requester"""
                for pins in self._reserved_pins.values():
                    if requester in pins:
                        pins.remove(requester)

            def close(self):
                for pins in self.pins.values():
                    pins.close()
                self.pins.clear()

        class LGPIOFactory(MockFactory):
            pass

__all__ = ['LGPIOFactory']

