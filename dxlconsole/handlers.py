from tornado.web import RequestHandler


class BaseRequestHandler(RequestHandler):
    """
    The base class for Tornado request handlers
    """

    def get_current_user(self):
        """
        Returns the current user for the request

        :return: The current user for the request
        """
        return self.get_secure_cookie("user")
