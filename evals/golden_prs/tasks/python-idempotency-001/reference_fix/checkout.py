class Checkout:
    def __init__(self, gateway):
        self.gateway = gateway
        self.completed = {}

    def charge(self, key, amount_cents):
        if key in self.completed:
            return self.completed[key]
        charge_id = self.gateway.charge(amount_cents)
        self.completed[key] = charge_id
        return charge_id
