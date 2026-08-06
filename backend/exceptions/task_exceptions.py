class SchedulerException(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)

    def __repr__(self):
        return self.message


class TaskAlreadyQueuedException(Exception):
    """Raised when a single-run job is enqueued while another job in its group is
    still running, queued, or waiting out a delay.

    `job_id` names the job that blocked it, when it is known. A submission
    rejected by the debounce window has no job to point at: the run that won the
    race may not have been enqueued yet.
    """

    def __init__(self, job_group: str, job_id: str | None = None):
        self.job_group = job_group
        self.job_id = job_id
        self.message = f"A '{job_group}' job is already queued or running"
        super().__init__(self.message)

    def __repr__(self):
        return self.message
