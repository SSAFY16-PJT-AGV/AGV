#include "ObjectInteractionHUD.h"

#include "AGVRobot.h"
#include "GameFramework/PlayerController.h"
#include "Engine/Canvas.h"
#include "Engine/Engine.h"
void AObjectInteractionHUD::DrawHUD()
{
    Super::DrawHUD();

    if (!Canvas)
    {
        return;
    }

    APlayerController* PC = GetOwningPlayerController();

    if (!PC)
    {
        return;
    }

    AAGVRobot* Robot = Cast<AAGVRobot>(PC->GetPawn());

    if (!Robot)
    {
        return;
    }

    FString Message;

    if (Robot->IsHoldingObject())
    {
        Message = TEXT("Right Click : Release Object");
    }
    else if (Robot->CanGrabObject())
    {
        Message = TEXT("Left Click : Grab Object");
    }
    else
    {
        return;
    }

    UFont* Font = GEngine->GetMediumFont();

    if (!Font)
    {
        return;
    }

    float TextWidth = 0.0f;
    float TextHeight = 0.0f;

    GetTextSize(
        Message,
        TextWidth,
        TextHeight,
        Font,
        1.2f
    );

    const float X = (Canvas->SizeX - TextWidth) * 0.5f;
    const float Y = Canvas->SizeY * 0.85f;

    DrawText(
        Message,
        FLinearColor::White,
        X,
        Y,
        Font,
        1.2f,
        false
    );

    const float CenterX = Canvas->SizeX * 0.5f;
    const float CenterY = Canvas->SizeY * 0.5f;

    const FLinearColor CrosshairColor =
        FLinearColor(0.0f, 1.0f, 1.0f, 0.9f);

    // 가로선
    DrawLine(
        CenterX - 7.0f,
        CenterY,
        CenterX + 7.0f,
        CenterY,
        CrosshairColor,
        2.0f
    );

    // 세로선
    DrawLine(
        CenterX,
        CenterY - 7.0f,
        CenterX,
        CenterY + 7.0f,
        CrosshairColor,
        2.0f
    );
}