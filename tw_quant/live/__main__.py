import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "tw_quant.live.api:create_app", host="0.0.0.0", port=8000,
        reload=False, factory=True,
    )
