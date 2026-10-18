//! Process trees are isolated so timeout cannot leave a benchmark running.
use anyhow::Result;
use std::{
    io::Read,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

pub fn isolate(command: &mut Command) {
    #[cfg(not(unix))]
    let _ = command;
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
}
pub fn kill_tree(child: &mut Child) {
    #[cfg(unix)]
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/F", "/T", "/PID", &child.id().to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    let _ = child.kill();
    let _ = child.wait();
}
pub struct ProcessOutput {
    pub stdout: String,
    pub stderr: String,
    pub code: i32,
    pub timed_out: bool,
}
pub fn run(command: &mut Command, timeout: Duration) -> Result<ProcessOutput> {
    isolate(command);
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn()?;
    #[cfg(windows)]
    let group = match WindowsJob::assign(&child) {
        Ok(group) => group,
        Err(error) => {
            kill_tree(&mut child);
            return Err(error);
        }
    };
    let mut stdout = child.stdout.take().unwrap();
    let mut stderr = child.stderr.take().unwrap();
    let out = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout.read_to_end(&mut bytes).map(|_| bytes)
    });
    let err = thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr.read_to_end(&mut bytes).map(|_| bytes)
    });
    let started = Instant::now();
    let mut timed_out = false;
    let mut exited = None;
    let status = loop {
        if exited.is_none() {
            exited = child.try_wait()?;
        }
        if let Some(status) = exited
            && out.is_finished()
            && err.is_finished()
        {
            break status;
        }
        if started.elapsed() >= timeout {
            timed_out = true;
            #[cfg(windows)]
            group.terminate();
            kill_tree(&mut child);
            break child.wait()?;
        }
        thread::sleep(Duration::from_millis(10));
    };
    let bytes = out
        .join()
        .map_err(|_| anyhow::anyhow!("stdout reader panicked"))??;
    let errors = err
        .join()
        .map_err(|_| anyhow::anyhow!("stderr reader panicked"))??;
    #[cfg(unix)]
    let code = {
        use std::os::unix::process::ExitStatusExt;
        status
            .code()
            .unwrap_or_else(|| -status.signal().unwrap_or(1))
    };
    #[cfg(not(unix))]
    let code = status.code().unwrap_or(-1);
    Ok(ProcessOutput {
        stdout: String::from_utf8_lossy(&bytes).into_owned(),
        stderr: String::from_utf8_lossy(&errors).into_owned(),
        code,
        timed_out,
    })
}

#[cfg(windows)]
struct WindowsJob(windows_sys::Win32::Foundation::HANDLE);
#[cfg(windows)]
impl WindowsJob {
    fn assign(child: &Child) -> Result<Self> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::*;
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            anyhow::ensure!(!handle.is_null(), "cannot create process job object");
            let result = Self(handle);
            let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            anyhow::ensure!(
                SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of_val(&info) as u32
                ) != 0,
                "cannot configure process job object"
            );
            anyhow::ensure!(
                AssignProcessToJobObject(handle, child.as_raw_handle()) != 0,
                "cannot isolate process tree"
            );
            Ok(result)
        }
    }
    fn terminate(&self) {
        unsafe {
            windows_sys::Win32::System::JobObjects::TerminateJobObject(self.0, 1);
        }
    }
}
#[cfg(windows)]
impl Drop for WindowsJob {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}
