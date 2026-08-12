class Users:
    def __init__(self):
        self.database = {1: "Old"}
        self.cache = {}

    def get(self, user_id):
        if user_id not in self.cache:
            self.cache[user_id] = self.database[user_id]
        return self.cache[user_id]

    def update(self, user_id, name):
        self.database[user_id] = name
