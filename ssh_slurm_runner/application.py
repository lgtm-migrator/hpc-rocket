import os
from typing import Optional, Tuple

from ssh_slurm_runner.environmentpreparation import EnvironmentPreparation
from ssh_slurm_runner.filesystemimpl import LocalFilesystem, SSHFilesystem
from ssh_slurm_runner.launchoptions import LaunchOptions
from ssh_slurm_runner.slurmrunner import SlurmError, SlurmJob, SlurmRunner
from ssh_slurm_runner.sshexecutor import SSHExecutor
from ssh_slurm_runner.ui import UI
from ssh_slurm_runner.watcher.jobwatcher import JobWatcher


class Application:

    def __init__(self, ui: UI) -> None:
        self._ui = ui
        self._latest_job_update: Optional[SlurmJob] = None
        self._runner: Optional[SlurmRunner] = None
        self._watcher: Optional[JobWatcher] = None
        self._jobid = 0

    def run(self, options: LaunchOptions) -> int:
        env_prep, success = self._prepare_environment(options)
        if not success:
            return 1

        executor = self._create_sshexecutor(options)
        self._runner = SlurmRunner(executor)
        self._launch_job(self._runner, options)
        self._wait_for_job_completion(options)

        self._collect_results(env_prep)
        self._clean_remote_environment(env_prep)
        executor.disconnect()

        return self._get_exit_code_for_job(self._latest_job_update)

    def _prepare_environment(self, options: LaunchOptions) -> Tuple[EnvironmentPreparation, bool]:
        self._ui.info("Preparing remote environment")
        env_prep = self._make_env_preparation(options)
        success = self._try_env_preparation(env_prep)
        if success:
            self._ui.success("Done")

        return env_prep, success

    def _try_env_preparation(self, env_prep: EnvironmentPreparation) -> bool:
        try:
            env_prep.prepare()
        except (FileNotFoundError, FileExistsError) as err:
            self._ui.error(self._get_error_message(err))
            env_prep.rollback()
            return False

        return True

    def _launch_job(self, runner: SlurmRunner, options: LaunchOptions) -> None:
        self._ui.info("Launching job")
        self._jobid = runner.sbatch(options.sbatch)
        self._ui.success(f"Job {self._jobid} launched")

    def _wait_for_job_completion(self, options: LaunchOptions) -> None:
        self._watcher = JobWatcher(self._runner)

        self._watcher.watch(self._jobid,
                            self._poll_callback,
                            options.poll_interval)

        self._watcher.wait_until_done()
        self._display_job_result()

    def _display_job_result(self):
        if self._latest_job_update.success:
            self._ui.success("Job completed successfully")
        else:
            self._ui.error("Job failed")

    def _clean_remote_environment(self, env_prep):
        self._ui.info("Cleaning up remote environment")
        env_prep.clean()
        self._ui.success("Done")

    def _collect_results(self, env_prep):
        self._ui.info("Collecting results")
        env_prep.collect()
        self._ui.success("Done")

    def _get_exit_code_for_job(self, job: SlurmJob) -> int:
        if job.success:
            return 0

        return 1

    def _make_env_preparation(self, options) -> EnvironmentPreparation:
        env_prep = EnvironmentPreparation(
            LocalFilesystem("."),
            self._make_ssh_filesystem(options))

        env_prep.files_to_copy(options.copy_files)
        env_prep.files_to_clean(options.clean_files)
        env_prep.files_to_collect(options.collect_files)

        return env_prep

    def _make_ssh_filesystem(self, options: LaunchOptions) -> SSHFilesystem:
        home_dir = os.environ['HOME']
        keyfile = self._resolve_keyfile_from_home_dir(options, home_dir)

        return SSHFilesystem(options.user,
                             options.host,
                             options.password,
                             options.private_key,
                             keyfile)

    def _poll_callback(self, job: SlurmJob) -> None:
        self._latest_job_update = job
        self._ui.update(job)

    def _create_sshexecutor(self, options) -> SSHExecutor:
        home_dir = os.environ['HOME']
        keyfile = self._resolve_keyfile_from_home_dir(options, home_dir)

        executor = SSHExecutor(options.host)
        executor.load_host_keys_from_file(f"{home_dir}/.ssh/known_hosts")

        executor.connect(options.user,
                         keyfile,
                         options.password,
                         options.private_key)

        return executor

    def _resolve_keyfile_from_home_dir(self, options, home_dir: str) -> str:
        keyfile = options.private_keyfile
        if keyfile.startswith("~/"):
            keyfile = keyfile.replace("~/", home_dir + "/", 1)

        return keyfile

    def _get_error_message(self, err: Exception) -> str:
        type_name = type(err).__name__
        error_string = f"{type_name}: {err}"
        return error_string

    def cancel(self) -> int:
        try:
            print(f"Canceling job {self._jobid}")
            self._runner.scancel(self._jobid)
            self._watcher.stop()
            job = self._runner.poll_status(self._jobid)
            self._ui.update(job)
        except SlurmError as err:
            print(err.args)

        return 1
