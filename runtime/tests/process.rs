use rvbench_runtime::process;
use std::{
    process::Command,
    time::{Duration, Instant},
};

fn fixture(mode: &str) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--ignored", "--exact", "child_fixture", "--nocapture"])
        .env("RVBENCH_CHILD_FIXTURE", mode);
    command
}
#[test]
fn captures_process_output() {
    let output = process::run(&mut fixture("echo"), Duration::from_secs(5)).unwrap();
    assert!(!output.timed_out);
    assert_eq!(output.code, 0);
    assert!(output.stdout.contains("native fixture output"));
}
#[test]
fn timeout_kills_child_tree_and_closes_inherited_pipes() {
    let started = Instant::now();
    let output = process::run(&mut fixture("spawn"), Duration::from_millis(200)).unwrap();
    assert!(output.timed_out);
    assert!(started.elapsed() < Duration::from_secs(3));
    assert!(output.stdout.contains("grandchild"));
}
#[test]
#[ignore = "invoked in a separate native process by the integration tests"]
fn child_fixture() {
    match std::env::var("RVBENCH_CHILD_FIXTURE").unwrap().as_str() {
        "echo" => println!("native fixture output"),
        "sleep" => std::thread::sleep(Duration::from_secs(10)),
        "spawn" => {
            std::thread::sleep(Duration::from_millis(20));
            let child = fixture("sleep").spawn().unwrap();
            println!("grandchild {}", child.id());
            std::process::exit(0);
        }
        _ => panic!("unknown fixture mode"),
    }
}
