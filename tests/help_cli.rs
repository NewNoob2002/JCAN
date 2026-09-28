use std::process::Command;

#[test]
fn help_is_available_without_hardware_and_respects_json() {
    for args in [
        vec!["--help"],
        vec!["-h"],
        vec!["help"],
        vec!["config-get", "--help"],
        vec!["config-set", "--help"],
        vec!["session", "-h"],
        vec!["--serial", "NO-SUCH-DEVICE", "send", "--help"],
    ] {
        for json in [false, true] {
            let mut command = Command::new(env!("CARGO_BIN_EXE_jcan"));
            command.args(&args);
            if json {
                command.arg("--json");
            }
            let output = command.output().unwrap();
            assert!(output.status.success(), "{args:?}: {output:?}");
            assert!(output.stderr.is_empty());
            let stdout = String::from_utf8(output.stdout).unwrap();
            let help = if json {
                assert_eq!(stdout.lines().count(), 1);
                let response: serde_json::Value = serde_json::from_str(&stdout).unwrap();
                assert_eq!(response["ok"], true);
                assert_eq!(response["operation"], "help");
                response["data"]["help"].as_str().unwrap().to_owned()
            } else {
                stdout
            };
            for text in [
                "Usage:",
                "config-set",
                "zero-based indices",
                "not individually verified",
                "fd_speed=06: 500 kbit/s; fd_speed=0A: 2 Mbit/s",
                "config-set data-speed 10",
                "ISO FD",
                "Bosch non-ISO FD",
                "custom timing overrides",
            ] {
                assert!(help.contains(text), "missing {text}: {args:?}");
            }
            let nominal = [
                5, 10, 20, 40, 50, 80, 100, 125, 200, 250, 300, 400, 500, 600, 800, 1,
            ];
            let data = [
                100, 125, 200, 250, 300, 400, 500, 600, 800, 1, 2, 3, 4, 5, 6, 8,
            ];
            let lines: Vec<String> = help
                .lines()
                .map(|line| line.split_whitespace().collect::<Vec<_>>().join(" "))
                .collect();
            for index in 0..16 {
                let nominal_unit = if index == 15 { "Mbit/s" } else { "kbit/s" };
                let data_unit = if index >= 9 { "Mbit/s" } else { "kbit/s" };
                let row = format!(
                    "{index:02X} {index} {} {nominal_unit} {} {data_unit}",
                    nominal[index], data[index]
                );
                assert!(lines.contains(&row), "missing mapping: {row}");
            }
        }
    }
}
