class Control:
    def __init__(self):
        self.sessions = []

    def send_test(self):
        for session in self.sessions:
            session.sendMessage(b'Test message from Control')