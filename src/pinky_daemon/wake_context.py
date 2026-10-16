"""Track which context snapshot reached the current transport process."""


class WakeContextDelivery:
    def __init__(self) -> None:
        self.generation = object()
        self.rendered_at = 0.0

    def new_process(self) -> None:
        self.generation = object()
        self.rendered_at = 0.0

    def snapshot(self) -> "WakeContextSnapshot":
        return WakeContextSnapshot(self)


class WakeContextSnapshot:
    def __init__(self, delivery: WakeContextDelivery) -> None:
        self._delivery = delivery
        self._generation = delivery.generation
        self.rendered_at = 0.0

    def delivered(self) -> None:
        # A late receipt from an earlier process cannot orient its replacement.
        if self._delivery.generation is self._generation:
            self._delivery.rendered_at = self.rendered_at
