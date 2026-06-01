function debounce(func, timeout=250, setValue) {
    let timer;
    return (...args) => {
        setValue(args[0].target.value);
        clearTimeout(timer);
        timer = setTimeout(() => { func.apply(this, args); }, timeout);
    }
}

export default function TextArea(props, context) {
    const [value, setValue] = React.useState(props.defaultValue || "");

     React.useEffect(() => {
        setValue(props.defaultValue || "");
    }, [props.defaultValue]);

    if (props.onInput) {
        let originalOnChange = props.onInput;
        if (props.onInput.name === "safeEventHandler") {
            props.onInput = debounce((e) => originalOnChange(e), 250, setValue);
        }
    }
    if (props.onEnterKey) {
        let originalOnEnterKey = props.onEnterKey;
        props.onKeyDown = function (e) {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                originalOnEnterKey(e);
                setValue("");  // Clear the textarea after sending the message
            }
        };
        delete props.onEnterKey;
    }
    props.value = value;
    return React.createElement("textarea", props);
};