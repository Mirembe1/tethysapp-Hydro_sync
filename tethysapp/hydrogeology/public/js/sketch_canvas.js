import { ReactSketchCanvas } from 'https://esm.sh/react-sketch-canvas?deps=react@19.0,react-dom@19.0&external=react,react-dom';
import {
    EraserFill, 
    ArrowCounterclockwise, 
    ArrowClockwise,
    PencilFill,
    Download,
    XCircleFill
} from 'https://esm.sh/react-bootstrap-icons?deps=react@19.0,react-dom@19.0&external=react,react-dom';

let debounceTimeout;

export default function SketchCanvas(props, context) {
    const sketchCanvasHandler = React.useRef(null);
    const onExportRef = React.useRef(props.onExport);
    const [eraseMode, setEraseMode] = React.useState(false);

    if (onExportRef.current != props.onExport) {
        onExportRef.current = props.onExport;
    }
    delete props.onExport;
    props.onChange = function () {
        window.clearTimeout(debounceTimeout);
        debounceTimeout = window.setTimeout(function () {
            sketchCanvasHandler.current?.exportImage("png").then((base64) => {
                if (base64) {
                    // 1. Remove the header (e.g., "data:image/png;base64,") if it exists
                    const base64Content = base64.split(',')[1] || base64;
                    
                    // 2. Decode base64 to binary string
                    const byteString = atob(base64Content);
                    let n = byteString.length;
                    const u8arr = new Uint8Array(n);
                    
                    while (n--) {
                        u8arr[n] = byteString.charCodeAt(n);
                    }
    
                    // 3. Create a File object
                    const file = new File([u8arr], "canvas.png", { type: "image/png" });
    
                    // 4. Use DataTransfer to programmatically set the FileList
                    const dataTransfer = new DataTransfer();
                    dataTransfer.items.add(file);
                    
                    const fileInput = document.getElementById("react-sketch-hidden-input");
                    fileInput.files = dataTransfer.files; // Assign the files to the input
                }
            });
        }, 1000);
    }

    function handleExport() {
        sketchCanvasHandler.current?.exportImage("png").then((base64) => {
            if (base64) {
                if (onExportRef.current && typeof onExportRef.current === "function") {
                    onExportRef.current(base64);
                } else {
                    const link = document.createElement("a");
                    link.href = base64;
                    link.download = "canvas.png";
                    document.body.appendChild(link);
                    link.click();
                    document.body.removeChild(link);
                }
            }
        });
    }

    function handleUndo() {
        sketchCanvasHandler.current?.undo();
    }

    function handleRedo() {
        sketchCanvasHandler.current?.redo();
    }

    function handleActivateEraser() {
        sketchCanvasHandler.current?.eraseMode(true);
        setEraseMode(true);
    }

    function handleActivatePencil() {
        sketchCanvasHandler.current?.eraseMode(false);
        setEraseMode(false);
    }

    function handleResetCanvas() {
        sketchCanvasHandler.current?.resetCanvas();
        setEraseMode(false);
    }

    props.ref = sketchCanvasHandler;

    return React.createElement("div", {style: {position: "relative"}}, [
        React.createElement(ReactSketchCanvas, props),
        React.createElement("input", {type: "file", id: "react-sketch-hidden-input", name: props.name || "sketch", style: {display: "none"}}),
        React.createElement(
            "div",
            {
                style: {
                    display: "flex",
                    gap: "0.2rem",
                }
            },
            [   
                React.createElement(
                    "button", 
                    {
                        onClick: handleExport
                    }, 
                    [
                        React.createElement(Download, {size: 20}),
                    ]
                ),
                React.createElement(
                    "button", 
                    {
                        onClick: handleUndo
                    }, 
                    [
                        React.createElement(ArrowCounterclockwise, {size: 20}),
                    ]
                ),
                React.createElement(
                    "button", 
                    {
                        onClick: handleRedo
                    }, 
                    [
                        React.createElement(ArrowClockwise, {size: 20}),
                    ]
                ),
                React.createElement(
                    "button", 
                    {
                        onClick: handleActivateEraser,
                        disabled: eraseMode
                    }, 
                    [
                        React.createElement(EraserFill, {size: 20}),
                    ]
                ),
                React.createElement(
                    "button", 
                    {
                        onClick: handleActivatePencil,
                        disabled: !eraseMode
                    }, 
                    [
                        React.createElement(PencilFill, {size: 20}),
                    ]
                ),
                React.createElement(
                    "button", 
                    {
                        onClick: handleResetCanvas,
                    }, 
                    [
                        React.createElement(XCircleFill, {size: 20}),
                    ]
                ),
            ]
        )
    ]);
}