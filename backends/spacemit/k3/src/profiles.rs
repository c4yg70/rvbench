use rvbench_core::BackendProfile;
pub fn profiles() -> Vec<BackendProfile> {
    [
        (
            "k3",
            "http://10.18.60.100:8788",
            Some("http://10.18.60.100:8791"),
            "k3",
            "k3-acc",
            "SpacemiT K3",
        ),
        (
            "k1",
            "http://10.18.60.241:8788",
            None,
            "k1",
            "k1",
            "SpacemiT K1, planned",
        ),
        (
            "sg2042",
            "",
            None,
            "sg2042",
            "sg2042-acc",
            "Sophgo SG2042, planned",
        ),
        (
            "rvv_general",
            "",
            None,
            "rvv_general",
            "rvv_general-acc",
            "RVV reference, planned",
        ),
    ]
    .into_iter()
    .map(|(name, base, corr, host, corrhost, notes)| BackendProfile {
        name: name.into(),
        timing_base: base.into(),
        corr_base: corr.map(str::to_owned),
        timing_host_tag: host.into(),
        corr_host_tag: corrhost.into(),
        warmup: 5,
        rep: 5,
        timeout_min: 15.,
        notes: notes.into(),
    })
    .collect()
}
pub fn get(name: &str) -> Option<BackendProfile> {
    profiles().into_iter().find(|p| p.name == name)
}
pub fn base_label(base: &str) -> String {
    let base = base.trim_end_matches('/');
    if base == "http://10.18.60.100:8788" {
        "k3".into()
    } else if base.ends_with(":8791") {
        "k3-acc".into()
    } else {
        base.into()
    }
}
pub fn comparable(a: Option<&str>, b: Option<&str>) -> bool {
    a.is_some_and(|s| !s.is_empty()) && a == b
}
