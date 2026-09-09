use serde_json::Value;
use std::io::Write;
use std::process::{Command, Stdio};

#[test]
fn session_handles_jsonl_and_shutdown_without_hardware() {
    let mut child = Command::new(env!("CARGO_BIN_EXE_jcan"))
        .args([
            "--serial",
            "NO-SUCH-DEVICE",
            "session",
            "--mode",
            "silent",
            "--reconnect-ms",
            "100",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(
            b"{\"id\":1,\"op\":\"ping\"}\n{\"id\":2,\"op\":\"status\"}\n{\"id\":3,\"op\":\"shutdown\"}\n",
        )
        .unwrap();
    drop(child.stdin.take());

    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let messages: Vec<Value> = String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert!(
        messages
            .iter()
            .any(|message| message["id"] == 1 && message["data"]["pong"] == true)
    );
    assert!(
        messages
            .iter()
            .any(|message| message["id"] == 2 && message["ok"] == true)
    );
    assert!(messages.iter().any(|message| {
        message["event"] == "session_started" && message["receive_enabled"] == false
    }));
    assert!(
        messages
            .iter()
            .any(|message| { message["id"] == 2 && message["data"]["receive_enabled"] == false })
    );
    assert!(
        messages
            .iter()
            .any(|message| message["id"] == 3 && message["data"]["stopped"] == true)
    );
}
