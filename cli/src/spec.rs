use crate::json::sha256;
use crate::{a, b, f, i, keys, names, require, s, schema_errors, set, unique};
use anyhow::{Result, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeSet, fs, path::Path};

pub fn walk<'a>(value: &'a Value, path: String, visit: &mut impl FnMut(&str, &'a Value)) {
    visit(&path, value);
    match value {
        Value::Object(map) => {
            for (key, item) in map {
                walk(item, format!("{path}.{key}"), visit);
            }
        }
        Value::Array(items) => {
            for (index, item) in items.iter().enumerate() {
                walk(item, format!("{path}[{index}]"), visit);
            }
        }
        _ => {}
    }
}

pub fn readiness_blockers(spec: &Value) -> Vec<String> {
    let mut errors = Vec::new();
    if spec["suite"]["status"] != "ready" {
        errors.push("suite.status is draft".into());
    }
    walk(spec, "$".into(), &mut |path, v| {
        // Baseline command/environment data is not part of case readiness.
        if path == "$.baseline" || path.starts_with("$.baseline.") {
            return;
        }
        if v.is_object()
            && (v["status"] == "pending" || v["kind"] == "pending" || v["method"] == "pending")
        {
            errors.push(format!("{path}: pending contract/output/check"));
        }
    });
    for op in a(&spec["operators"]) {
        for case in a(&op["cases"]) {
            let state = s(&case["execution"]["status"]);
            if state != "ready" && !(state == "skip" && case["slot"] != "C01") {
                errors.push(format!("{}: execution is {state}", s(&case["id"])));
            }
        }
    }
    errors
}

#[derive(Clone, Debug)]
pub struct ExpectedTensor {
    pub name: String,
    pub shape: Vec<i64>,
    pub dtype: String,
}
fn shape(value: &Value) -> Result<Vec<i64>> {
    a(value)
        .iter()
        .map(|v| {
            v.as_i64()
                .or_else(|| {
                    v.as_f64()
                        .filter(|x| x.fract() == 0.0 && *x >= 0.0 && *x < i64::MAX as f64)
                        .map(|x| x as i64)
                })
                .ok_or_else(|| anyhow::anyhow!("invalid shape dimension"))
        })
        .collect()
}
fn dim(value: &Value, rank: usize) -> Result<usize> {
    let d = value
        .as_i64()
        .ok_or_else(|| anyhow::anyhow!("dim must be an integer"))?;
    ensure!(
        rank > 0 && d >= -(rank as i64) && d < rank as i64,
        "dim is outside input rank"
    );
    Ok(d.rem_euclid(rank as i64) as usize)
}
fn pair(value: &Value, name: &str, minimum: i64) -> Result<[i64; 2]> {
    let values = if let Some(n) = value.as_i64() {
        vec![n, n]
    } else {
        a(value).iter().map(|v| v.as_i64().unwrap_or(-1)).collect()
    };
    ensure!(
        values.len() == 2 && values.iter().all(|n| *n >= minimum),
        "{name} must be an integer/pair >= {minimum}"
    );
    Ok([values[0], values[1]])
}

pub fn expected_tensor_outputs(op: &Value, case: &Value) -> Result<Option<Vec<ExpectedTensor>>> {
    let id = s(&op["id"]);
    let expected_attrs: &[&str] = match id {
        "abs" | "mv" | "mm" => &[],
        "aminmax" => &["dim", "keepdim"],
        "softmax" => &["dim", "half_to_float"],
        "topk" => &["k", "dim", "largest", "sorted"],
        "conv2d" => &["stride", "padding", "dilation", "groups"],
        "_flash_attention_forward" => &[
            "cumulative_sequence_length_q",
            "cumulative_sequence_length_k",
            "max_q",
            "max_k",
            "dropout_p",
            "is_causal",
            "return_debug_mask",
            "scale",
            "window_size_left",
            "window_size_right",
            "seqused_k",
            "alibi_slopes",
        ],
        _ => return Ok(None),
    };
    ensure!(
        keys(&case["attrs"]) == expected_attrs.iter().map(|v| v.to_string()).collect(),
        "{id} attrs must match operator contract"
    );
    let inputs = a(&case["inputs"]);
    let mut tensors = Vec::new();
    for binding in a(&op["call"]["args"]) {
        if binding["kind"] == "input" {
            tensors.push(
                inputs
                    .iter()
                    .find(|v| v["name"] == binding["name"])
                    .ok_or_else(|| anyhow::anyhow!("missing input binding"))?,
            );
        }
    }
    let count = match id {
        "mv" | "mm" => 2,
        "conv2d" | "_flash_attention_forward" => 3,
        _ => 1,
    };
    ensure!(
        tensors.len() == count,
        "{id} requires {count} ordered tensor/none bindings"
    );
    ensure!(
        tensors
            .iter()
            .take(if count > 1 { 2 } else { 1 })
            .all(|t| t["kind"] == "tensor"),
        "required tensor cannot be none"
    );
    let attrs = &case["attrs"];
    for name in [
        "keepdim",
        "half_to_float",
        "largest",
        "sorted",
        "is_causal",
        "return_debug_mask",
    ] {
        if attrs.get(name).is_some() {
            ensure!(attrs[name].is_boolean(), "{name} must be boolean");
        }
    }
    let dtype = s(&tensors[0]["dtype"]);
    ensure!(
        tensors
            .iter()
            .filter(|t| t["kind"] == "tensor")
            .all(|t| s(&t["dtype"]) == dtype),
        "v1 numeric reference requires same dtype for tensor inputs"
    );
    let input = shape(&tensors[0]["shape"])?;
    let output = |name: &str, shape: Vec<i64>, dtype: &str| ExpectedTensor {
        name: name.into(),
        shape,
        dtype: dtype.into(),
    };
    let values = match id {
        "abs" => vec![output("out", input, dtype)],
        "aminmax" => {
            ensure!(!input.contains(&0), "aminmax input cannot be empty");
            let result = if attrs["dim"].is_null() {
                if b(&attrs["keepdim"]) {
                    vec![1; input.len()]
                } else {
                    vec![]
                }
            } else {
                let d = dim(&attrs["dim"], input.len())?;
                let mut v = input.clone();
                if b(&attrs["keepdim"]) {
                    v[d] = 1;
                } else {
                    v.remove(d);
                }
                v
            };
            vec![
                output("min", result.clone(), dtype),
                output("max", result, dtype),
            ]
        }
        "softmax" => {
            dim(&attrs["dim"], input.len())?;
            vec![output(
                "out",
                input,
                if b(&attrs["half_to_float"]) {
                    "float32"
                } else {
                    dtype
                },
            )]
        }
        "mv" | "mm" => {
            let other = shape(&tensors[1]["shape"])?;
            ensure!(
                input.len() == 2
                    && other.len() == if id == "mv" { 1 } else { 2 }
                    && input[1] == other[0],
                "matrix input ranks or contraction dimensions disagree"
            );
            vec![output(
                "out",
                if id == "mv" {
                    vec![input[0]]
                } else {
                    vec![input[0], other[1]]
                },
                dtype,
            )]
        }
        "topk" => {
            let d = dim(&attrs["dim"], input.len())?;
            let k = attrs["k"]
                .as_i64()
                .ok_or_else(|| anyhow::anyhow!("k must be an integer"))?;
            ensure!(
                k >= 0 && k <= input[d],
                "k must be an integer in [0, shape[dim]]"
            );
            let mut result = input;
            result[d] = k;
            vec![
                output("values", result.clone(), dtype),
                output("indices", result, "int64"),
            ]
        }
        "conv2d" => {
            let w = shape(&tensors[1]["shape"])?;
            let groups = i(&attrs["groups"]);
            ensure!(
                input.len() == 4 && w.len() == 4 && groups > 0,
                "conv2d requires NCHW/OIHW tensors and positive groups"
            );
            ensure!(
                i128::from(input[1]) == i128::from(w[1]) * i128::from(groups)
                    && w[0] % groups == 0
                    && input[1] >= 1
                    && w.iter().all(|n| *n >= 1),
                "conv2d grouped channel dimensions disagree"
            );
            if tensors[2]["kind"] == "tensor" {
                ensure!(
                    shape(&tensors[2]["shape"])? == vec![w[0]],
                    "conv2d bias shape must equal [out_channels]"
                );
            }
            let stride = pair(&attrs["stride"], "stride", 1)?;
            let dilation = pair(&attrs["dilation"], "dilation", 1)?;
            let spatial = if attrs["padding"] == "same" {
                ensure!(stride == [1, 1], "same padding requires stride=1");
                vec![input[2], input[3]]
            } else {
                let pad = if attrs["padding"] == "valid" {
                    [0, 0]
                } else {
                    pair(&attrs["padding"], "padding", 0)?
                };
                let mut v = Vec::new();
                for axis in 0..2 {
                    let size = (i128::from(input[axis + 2]) + 2 * i128::from(pad[axis])
                        - i128::from(dilation[axis]) * (i128::from(w[axis + 2]) - 1)
                        - 1)
                    .div_euclid(i128::from(stride[axis]))
                        + 1;
                    ensure!(
                        size > 0 && size <= i128::from(i64::MAX),
                        "conv2d output spatial size must be positive"
                    );
                    v.push(size as i64);
                }
                v
            };
            vec![output(
                "out",
                vec![input[0], w[0], spatial[0], spatial[1]],
                dtype,
            )]
        }
        _ => {
            ensure!(tensors[2]["kind"] == "tensor", "value must be a tensor");
            let k = shape(&tensors[1]["shape"])?;
            let v = shape(&tensors[2]["shape"])?;
            ensure!(
                input.len() == 4
                    && k.len() == 4
                    && v.len() == 4
                    && k == v
                    && input[0] == k[0]
                    && input[2..] == k[2..],
                "v1 attention requires compatible BSHD Q/K/V without GQA"
            );
            ensure!(
                input
                    .iter()
                    .chain(k.iter())
                    .chain(v.iter())
                    .all(|n| *n >= 1),
                "v1 attention dimensions must be positive"
            );
            ensure!(
                attrs["max_q"].as_i64() == Some(input[1]) && attrs["max_k"].as_i64() == Some(k[1]),
                "attention max_q/max_k must match actual sequence lengths"
            );
            ensure!(
                [
                    "cumulative_sequence_length_q",
                    "cumulative_sequence_length_k",
                    "window_size_left",
                    "window_size_right",
                    "seqused_k",
                    "alibi_slopes"
                ]
                .iter()
                .all(|n| attrs[*n].is_null())
                    && f(&attrs["dropout_p"]) == 0.0
                    && !b(&attrs["return_debug_mask"]),
                "v1 attention supports fixed length, no dropout/window/alibi/debug mask"
            );
            ensure!(
                f(&attrs["scale"]).is_finite(),
                "attention scale must be finite"
            );
            ensure!(
                !b(&attrs["is_causal"]) || input[1] == k[1],
                "v1 causal attention requires square sequence lengths"
            );
            ensure!(
                tensors.iter().all(
                    |t| t["layout"]["axes"] == json!(["batch", "sequence", "head", "head_dim"])
                ),
                "attention inputs must explicitly declare BSHD axes"
            );
            vec![output("out", input, dtype)]
        }
    };
    Ok(Some(values))
}

pub fn validate_spec(spec: &Value, base_dir: Option<&Path>) -> Vec<String> {
    let mut errors = schema_errors(spec, "spec");
    if !errors.is_empty() {
        return errors;
    }
    walk(spec, "$".into(), &mut |path, v| {
        if path == "$.baseline" || path.starts_with("$.baseline.") {
            return;
        }
        if v.is_object() && v["status"] == "resolved" && v.get("sha256").is_some() {
            let relative = s(&v["path"]);
            let p = Path::new(relative);
            if p.is_absolute() || relative.contains(':') {
                errors.push(format!(
                    "{path}: contract path must be relative to spec.json"
                ));
            } else if let Some(base) = base_dir {
                match fs::read(base.join(p)) {
                    Ok(bytes) => require(
                        &mut errors,
                        sha256(&bytes) == s(&v["sha256"]),
                        format!("{path}: contract sha256 mismatch"),
                    ),
                    Err(_) => errors.push(format!("{path}: contract file does not exist")),
                }
            } else {
                errors.push(format!("{path}: base_dir required to resolve contract"));
            }
        }
    });
    let sources = names(&spec["sources"], "id");
    let source_set = set(&sources);
    unique(&mut errors, sources, "source id");
    unique(&mut errors, names(&spec["operators"], "id"), "operator id");
    for source in a(&spec["sources"]) {
        let status = s(&source["revision_status"]);
        if ["abbreviated", "pinned"].contains(&status) {
            require(
                &mut errors,
                !s(&source["revision"]).is_empty(),
                "known source revision requires a value",
            );
        }
        if ["unresolved", "not_applicable"].contains(&status) {
            require(
                &mut errors,
                source["revision"].is_null(),
                "unresolved/not_applicable revision must be null",
            );
        }
    }
    let all_cases: Vec<String> = a(&spec["operators"])
        .iter()
        .flat_map(|op| names(&op["cases"], "id"))
        .collect();
    require(
        &mut errors,
        all_cases.len() as i64 <= i(&spec["suite"]["coverage"]["planned_case_count"]),
        "declared cases exceed planned_case_count",
    );
    unique(&mut errors, all_cases, "case id");
    let float_types = ["float16", "bfloat16", "float32", "float64"];
    for op in a(&spec["operators"]) {
        let id = s(&op["id"]);
        let returns = &op["returns"];
        if returns["kind"] == "tensor" {
            require(
                &mut errors,
                a(&returns["names"]).len() == 1,
                "tensor return must have exactly one name",
            );
        }
        let expected: Option<Vec<&str>> = match id {
            "abs" | "softmax" | "mv" | "mm" | "conv2d" => Some(vec!["out"]),
            "aminmax" => Some(vec!["min", "max"]),
            "topk" => Some(vec!["values", "indices"]),
            "_flash_attention_forward" => {
                Some(vec!["out", "lse", "philox_seed", "philox_offset", "p"])
            }
            _ => None,
        };
        if let Some(expected) = expected {
            require(
                &mut errors,
                returns
                    == &json!({"kind":if expected.len()==1{"tensor"}else{"tuple"},"names":expected}),
                format!("{id}: return structure does not match operator contract"),
            );
        }
        unique(&mut errors, names(&op["cases"], "slot"), "case slot");
        for case in a(&op["cases"]) {
            let label = s(&case["id"]);
            require(
                &mut errors,
                a(&case["provenance"]["source_refs"])
                    .iter()
                    .all(|v| source_set.contains(s(v))),
                format!("{label}: provenance refers to an undefined source"),
            );
            require(
                &mut errors,
                label == format!("{}.{}.{}", s(&spec["suite"]["id"]), id, s(&case["slot"])),
                format!("{label}: case id must be suite.operator.slot"),
            );
            let input_names = names(&case["inputs"], "name");
            let output_names = names(&case["outputs"], "name");
            unique(&mut errors, input_names.clone(), "input names");
            unique(&mut errors, output_names.clone(), "output names");
            require(
                &mut errors,
                case["outputs"]
                    .as_array()
                    .map(|_| json!(output_names.clone()))
                    == Some(returns["names"].clone()),
                format!("{label}: ordered outputs do not match return slots"),
            );
            let mut bound_input = BTreeSet::new();
            let mut bound_attr = BTreeSet::new();
            for binding in a(&op["call"]["args"]).iter().chain(
                op["call"]["kwargs"]
                    .as_object()
                    .into_iter()
                    .flat_map(|v| v.values()),
            ) {
                if binding["kind"] == "input" {
                    bound_input.insert(s(&binding["name"]).to_owned());
                } else {
                    bound_attr.insert(s(&binding["name"]).to_owned());
                }
            }
            require(
                &mut errors,
                bound_input == set(&input_names) && bound_attr == keys(&case["attrs"]),
                format!("{label}: call bindings must cover exactly the declared inputs and attrs"),
            );
            for item in a(&case["inputs"]).iter().chain(a(&case["outputs"])) {
                if item["kind"] != "tensor" {
                    continue;
                }
                let rank = a(&item["shape"]).len();
                let axes: Vec<String> = a(&item["layout"]["axes"])
                    .iter()
                    .map(|v| s(v).to_owned())
                    .collect();
                require(
                    &mut errors,
                    axes.len() == rank && set(&axes).len() == rank,
                    format!("{label}: axes must be unique and match rank"),
                );
                if let Some(strides) = item["layout"].get("strides") {
                    require(
                        &mut errors,
                        a(strides).len() == rank,
                        format!("{label}: strides must match rank"),
                    );
                }
                if item["layout"]["kind"] == "contiguous" {
                    require(
                        &mut errors,
                        item["layout"].get("strides").is_none()
                            && item["layout"]
                                .get("storage_offset")
                                .is_none_or(|v| i(v) == 0),
                        format!("{label}: contiguous layout uses computed strides and offset=0"),
                    );
                }
                if item["generator"]["kind"] == "torch_cpu_uniform_v1" {
                    require(
                        &mut errors,
                        f(&item["generator"]["low"]) < f(&item["generator"]["high"])
                            && float_types.contains(&s(&item["dtype"])),
                        format!("{label}: uniform requires low < high and floating dtype"),
                    );
                }
            }
            let checks = &case["verification"]["output_checks"];
            let check_names = names(checks, "output");
            require(
                &mut errors,
                set(&check_names) == set(&output_names) && check_names.len() == output_names.len(),
                format!("{label}: exactly one output check is required per return slot"),
            );
            for check in a(checks) {
                let item = a(&case["outputs"])
                    .iter()
                    .find(|o| o["name"] == check["output"]);
                let method = s(&check["method"]);
                if method == "allclose" {
                    require(
                        &mut errors,
                        !check["contract"].is_null()
                            && item.is_some_and(|o| float_types.contains(&s(&o["dtype"]))),
                        format!("{label}: allclose needs a floating output and numeric contract"),
                    );
                } else {
                    require(
                        &mut errors,
                        check["contract"].is_null(),
                        format!("{label}: only allclose uses numeric contract"),
                    );
                }
                if method == "pending" || item.is_some_and(|o| o["kind"] == "pending") {
                    require(
                        &mut errors,
                        method == "pending" && !s(&check["reason"]).is_empty(),
                        format!("{label}: pending output/check requires reason and pending method"),
                    );
                }
            }
            let rules = &case["verification"]["semantic_rules"];
            let rule_ids = names(rules, "id");
            unique(&mut errors, rule_ids.clone(), "semantic rules");
            if id == "topk" {
                let correct = a(checks).iter().all(|c| {
                    (c["output"] == "values" && c["method"] == "topk_values")
                        || (c["output"] == "indices" && c["method"] == "topk_indices")
                });
                require(
                    &mut errors,
                    correct && rule_ids == vec!["topk_selection_v1"],
                    format!(
                        "{label}: TopK requires tie-aware values and exact semantic indices checks"
                    ),
                );
            }
            if id == "softmax" {
                require(
                    &mut errors,
                    rule_ids == vec!["softmax_probability_v1"],
                    format!("{label}: Softmax requires probability/normalization check"),
                );
            }
            for rule in a(rules) {
                if rule["id"] == "softmax_probability_v1" {
                    require(
                        &mut errors,
                        id == "softmax" && !rule["contract"].is_null(),
                        format!("{label}: softmax normalization needs its own contract"),
                    );
                }
                if rule["id"] == "topk_selection_v1" {
                    require(
                        &mut errors,
                        id == "topk" && rule["contract"].is_null(),
                        format!("{label}: TopK selection is an exact semantic rule"),
                    );
                }
            }
            let execution = &case["execution"];
            let state = s(&execution["status"]);
            if state != "ready" {
                require(
                    &mut errors,
                    !s(&execution["reason"]).is_empty(),
                    format!("{label}: non-ready execution needs reason"),
                );
            }
            if ["blocked", "skip", "ready"].contains(&state) {
                require(
                    &mut errors,
                    !a(&execution["evidence"]).is_empty(),
                    format!("{label}: execution needs support/probe evidence"),
                );
            }
            if bound_input == set(&input_names) {
                match expected_tensor_outputs(op, case) {
                    Ok(Some(outputs)) => {
                        for expected in outputs {
                            let actual = a(&case["outputs"])
                                .iter()
                                .find(|v| s(&v["name"]) == expected.name);
                            require(
                                &mut errors,
                                actual.is_some_and(|v| {
                                    v["kind"] == "tensor"
                                        && shape(&v["shape"]).ok() == Some(expected.shape.clone())
                                        && s(&v["dtype"]) == expected.dtype
                                }),
                                format!(
                                    "{label}: output {} must match shape and dtype",
                                    expected.name
                                ),
                            );
                        }
                    }
                    Ok(None) => {}
                    Err(e) => errors.push(format!("{label}: {e}")),
                }
            }
        }
    }
    if spec["suite"]["status"] == "ready" {
        require(
            &mut errors,
            readiness_blockers(spec).is_empty(),
            "suite.status=ready cannot contain pending contracts or unready cases",
        );
    }
    errors.extend(crate::baseline::validate_baseline(spec, base_dir));
    errors
}
