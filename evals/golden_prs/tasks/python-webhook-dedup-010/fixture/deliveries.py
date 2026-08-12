class Deliveries:
    def __init__(self):
        self.seen = set()

    def process(self, delivery_id, handler):
        if delivery_id in self.seen:
            return False
        handler()
        self.seen.add(delivery_id)
        return True
